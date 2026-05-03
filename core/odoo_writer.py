"""
Modulo di scrittura Odoo per l'agent.

Gestisce la creazione di bozze account.move e l'aggiornamento di
purchase.order.line per l'automazione "Gradino 1" (bozze pre-compilate).

Separato da odoo_client.py per chiarezza: odoo_client.py è read-only,
odoo_writer.py è read-write.

Safety features:
- DRY_RUN mode: logga operazioni senza scriverle
- Idempotenza: verifica se bozza già esiste prima di crearne un'altra
- Rollback: funzione per cancellare bozze create per errore
- Validazione: verifica pre-scrittura di tutti i riferimenti

Uso tipico:
    writer = OdooWriter(client, dry_run=True)
    result = writer.create_bozza_fornitore_fisso(analysis, mapping_entry)
    if result['success']:
        log(f"Bozza creata: move_id={result['move_id']}")
"""

import base64
import calendar
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class WriteResult:
    """Risultato di un'operazione di scrittura."""
    success: bool
    action: str                         # 'create_draft', 'rollback', 'update_line'
    move_id: Optional[int] = None       # id del account.move creato
    po_line_id: Optional[int] = None    # id della purchase.order.line aggiornata
    old_price_unit: Optional[float] = None  # per rollback
    old_name: Optional[str] = None          # per rollback
    old_date_planned: Optional[str] = None  # per rollback (YYYY-MM-DD HH:MM:SS)
    error_message: Optional[str] = None
    dry_run: bool = False
    # Multi-linea: righe OdA aggiuntive consumate/create (oltre a po_line_id)
    extra_po_lines: Optional[List[Dict]] = None

    def to_dict(self) -> Dict:
        d = {
            'success': self.success,
            'action': self.action,
            'move_id': self.move_id,
            'po_line_id': self.po_line_id,
            'old_price_unit': self.old_price_unit,
            'old_name': self.old_name,
            'old_date_planned': self.old_date_planned,
            'error_message': self.error_message,
            'dry_run': self.dry_run,
        }
        if self.extra_po_lines:
            d['extra_po_lines'] = self.extra_po_lines
        return d


class OdooWriter:
    """
    Gestisce scritture Odoo in modo controllato e reversibile.
    """

    def __init__(self, client, dry_run: bool = True):
        """
        Args:
            client: OdooReadOnlyClient (anche se scriviamo, usiamo i suoi metodi XML-RPC)
            dry_run: se True non scrive realmente, logga solo le azioni
        """
        self.client = client
        self.dry_run = dry_run

    # === Helpers === #

    def _find_libere_purchase_order_lines(self, po_id: int,
                                           criterio: str = 'standard_qty_inv_rec') -> List[Dict]:
        """
        Ritorna le righe 'libere' di un OdA secondo il criterio specificato.

        Criteri supportati:
        - 'standard_qty_inv_rec' (default): qty_invoiced=0 AND qty_received=0
          AND product_qty>=1. Prezzo e descrizione non contano. Adatta per
          OdA-ledger ricorrenti (Trenitalia P03524, Italo P04279).
        - 'price_zero_only' (legacy): price_unit=0 AND qty_invoiced=0.
          Retrocompatibilità se qualcuno non vuole il nuovo criterio.
        """
        lines = self.client._call('purchase.order.line', 'search_read',
            [('order_id', '=', po_id)],
            fields=['id', 'name', 'product_id', 'product_qty',
                    'price_unit', 'qty_invoiced', 'qty_received',
                    'taxes_id', 'account_analytic_id', 'date_planned',
                    'product_uom'])

        if criterio == 'price_zero_only':
            libere = [l for l in lines
                      if (l.get('price_unit') or 0) == 0
                      and (l.get('qty_invoiced') or 0) == 0]
        else:  # default: standard_qty_inv_rec
            libere = [l for l in lines
                      if (l.get('qty_invoiced') or 0) == 0
                      and (l.get('qty_received') or 0) == 0
                      and (l.get('product_qty') or 0) >= 1]

        # Ordino per prezzo crescente: se ci sono più righe libere, consumo
        # prima quelle con prezzo più basso (tipicamente righe "test"/plafond
        # minimi). Questo protegge eventuali righe di valore alto che avessero
        # per caso qty_inv=0 e qty_rec=0.
        libere.sort(key=lambda l: (l.get('price_unit') or 0, l['id']))
        return libere

    def _validate_mapping(self, mapping_entry: Dict) -> Optional[str]:
        """
        Verifica che la mappatura sia completa e valida.
        Ritorna None se OK, o stringa errore.
        """
        required = ['oda_fisso', 'partner_id', 'conto_contabile_id',
                    'taxes_id', 'journal_id', 'company_id']
        for k in required:
            if k not in mapping_entry or mapping_entry[k] is None:
                return f"Mappatura incompleta: campo '{k}' mancante"
        return None

    def _build_line_description(self, analysis) -> str:
        """
        Costruisce la descrizione della riga OdA dalla fattura XML.
        Format: TRATTA1 + TRATTA2 + ... (Tit. COD1, COD2, ..., DATA)
        """
        if not analysis.xml_data:
            return "Fattura senza dati XML"

        d = analysis.xml_data
        righe = d.righe or []

        # Concateno le descrizioni delle righe (tratte)
        tratte = [r.descrizione.strip() for r in righe
                  if r.descrizione and r.descrizione.strip()]
        tratte_str = " + ".join(tratte) if tratte else "Fornitura di Servizi"

        # Estraggo i codici titolo dai dati gestionali dell'XML
        # I codici sono in AltriDatiGestionali/RiferimentoTesto con TipoDato "Tit. n.1"
        codici = self._extract_codici_titoli(analysis)
        codici_str = ", ".join(codici) if codici else ""

        # Data formato DD/MM/YYYY
        data_str = ''
        if d.data:
            parts = d.data.split('-')
            if len(parts) == 3:
                data_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
            else:
                data_str = d.data

        # Costruisco la descrizione finale
        parts = [tratte_str]
        ref_parts = []
        if codici_str:
            ref_parts.append(f"Tit. {codici_str}")
        if data_str:
            ref_parts.append(data_str)
        if ref_parts:
            parts.append(f"({', '.join(ref_parts)})")

        return " ".join(parts)

    def _extract_codici_titoli(self, analysis) -> List[str]:
        """
        Estrae i codici dei titoli di viaggio dall'XML (AltriDatiGestionali).
        """
        if not analysis.raw_xml:
            return []

        import re
        # Cerco pattern <TipoDato>Tit. n.X</TipoDato><RiferimentoTesto>YYYY</RiferimentoTesto>
        pattern = r'<TipoDato>Tit[^<]*</TipoDato>\s*<RiferimentoTesto>([^<]+)</RiferimentoTesto>'
        matches = re.findall(pattern, analysis.raw_xml)
        return [m.strip() for m in matches if m.strip()]

    def _build_line_description_pass_through(self, analysis) -> str:
        """
        Strategia 'pass_through' per fornitori come Italo: la descrizione XML
        della riga contiene già codice prenotazione + tratta + (eventuale
        viaggiatore), quindi la concateno pari pari e aggiungo la data.
        
        Esempio Italo:
          XML riga 1: 'ZH91WG - Roma Termini - Milano Centrale'
          XML riga 2: 'ZH91WG - Milano Centrale - Roma Termini'
          -> 'ZH91WG - Roma Termini - Milano Centrale + ZH91WG - Milano Centrale - Roma Termini (15/04/2026)'
        """
        if not analysis.xml_data:
            return "Fattura senza dati XML"

        d = analysis.xml_data
        righe = d.righe or []
        tratte = [r.descrizione.strip() for r in righe
                  if r.descrizione and r.descrizione.strip()]
        tratte_str = " + ".join(tratte) if tratte else "Fornitura di Servizi"

        # Data in formato DD/MM/YYYY
        data_str = ''
        if d.data:
            parts = d.data.split('-')
            if len(parts) == 3:
                data_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
            else:
                data_str = d.data

        return f"{tratte_str} ({data_str})" if data_str else tratte_str

    def _build_line_description_nc_pass_through(self, analysis) -> str:
        """
        Come _build_line_description_pass_through ma per le note credito.
        Prefisso 'NC' + eventuale riferimento fattura originale.
        """
        base = self._build_line_description_pass_through(analysis)
        desc = f"NC - {base}"
        rif = self._extract_rif_fattura_originale(analysis)
        if rif:
            desc += f" rif.ft {rif}"
        return desc

    def _build_line_description_nwg_periodo(self, analysis) -> str:
        """
        Strategia 'nwg_periodo' per NWG Energia: ricostruisce il pattern
        usato manualmente sulle righe OdA P01178:
          'Fornitura <IdContratto> da DD/MM/YYYY a DD/MM/YYYY'
        Periodo = mese pieno della data fattura (NWG fattura mensilmente).
        """
        if not analysis.xml_data:
            return "Fornitura"

        d = analysis.xml_data
        contratto = ''
        if getattr(d, 'contratto_riferimenti', None):
            contratto = d.contratto_riferimenti[0]

        # Periodo = primo/ultimo giorno del mese della data fattura
        periodo_str = ''
        if d.data:
            try:
                import calendar
                y, m, _day = d.data.split('-')
                y_i, m_i = int(y), int(m)
                last = calendar.monthrange(y_i, m_i)[1]
                periodo_str = (f"da 01/{m_i:02d}/{y_i} "
                               f"a {last:02d}/{m_i:02d}/{y_i}")
            except Exception:
                pass

        if contratto and periodo_str:
            return f"Fornitura {contratto} {periodo_str}"
        if contratto:
            return f"Fornitura {contratto}"
        return periodo_str or "Fornitura"

    def _build_line_description_nc_nwg_periodo(self, analysis) -> str:
        """Variante NC della strategia nwg_periodo."""
        base = self._build_line_description_nwg_periodo(analysis)
        desc = f"NC - {base}"
        rif = self._extract_rif_fattura_originale(analysis)
        if rif:
            desc += f" rif.ft {rif}"
        return desc

    def _build_description_by_strategy(self, analysis, strategy: str,
                                       is_nota_credito: bool) -> str:
        """
        Dispatcher delle strategie di descrizione in base al fornitore.
        Se il fornitore ha una strategia non riconosciuta, ricade sul
        default 'trenitalia_titoli' (con avviso nel log).
        """
        if strategy == 'keep_original':
            return None  # segnale per non sovrascrivere la descrizione OdA
        elif strategy == 'pass_through':
            if is_nota_credito:
                return self._build_line_description_nc_pass_through(analysis)
            return self._build_line_description_pass_through(analysis)
        elif strategy == 'nwg_periodo':
            if is_nota_credito:
                return self._build_line_description_nc_nwg_periodo(analysis)
            return self._build_line_description_nwg_periodo(analysis)
        elif strategy == 'trenitalia_titoli':
            if is_nota_credito:
                return self._build_line_description_nc(analysis)
            return self._build_line_description(analysis)
        else:
            logger.warning(f"Strategy '{strategy}' non riconosciuta, "
                          f"uso default trenitalia_titoli")
            if is_nota_credito:
                return self._build_line_description_nc(analysis)
            return self._build_line_description(analysis)

    def _extract_rif_fattura_originale(self, analysis) -> Optional[str]:
        """
        Per le NC (TD04), estrae il numero della fattura originale cui si
        riferisce la nota credito. L'XML Trenitalia lo riporta come:
          <AltriDatiGestionali>
            <TipoDato> FATTURA</TipoDato>
            <RiferimentoTesto>2026/9000994299</RiferimentoTesto>
          </AltriDatiGestionali>
        Ritorna la stringa o None se non trovata.
        """
        if not analysis.raw_xml:
            return None
        import re
        # Pattern tollerante a spazi/case: TipoDato può essere "FATTURA" o " FATTURA"
        pattern = (r'<TipoDato>\s*FATTURA\s*</TipoDato>\s*'
                   r'<RiferimentoTesto>([^<]+)</RiferimentoTesto>')
        matches = re.findall(pattern, analysis.raw_xml, re.IGNORECASE)
        if matches:
            return matches[0].strip()
        return None

    def _build_line_description_nc(self, analysis) -> str:
        """
        Costruisce la descrizione della riga OdA per una nota credito.
        Format: NC - TRATTA1 + TRATTA2 (Tit. COD1, COD2, DATA) rif.ft NUMERO_ORIGINALE
        Se manca il riferimento alla fattura originale, il 'rif.ft' viene omesso.
        """
        if not analysis.xml_data:
            return "NC - Fattura senza dati XML"

        d = analysis.xml_data
        righe = d.righe or []

        # Concateno le descrizioni delle righe (tratte)
        tratte = [r.descrizione.strip() for r in righe
                  if r.descrizione and r.descrizione.strip()]
        tratte_str = " + ".join(tratte) if tratte else "Fornitura di Servizi"

        # Codici titoli
        codici = self._extract_codici_titoli(analysis)
        codici_str = ", ".join(codici) if codici else ""

        # Data formato DD/MM/YYYY
        data_str = ''
        if d.data:
            parts = d.data.split('-')
            if len(parts) == 3:
                data_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
            else:
                data_str = d.data

        # Costruisco parentetica (Tit. + data)
        ref_parts = []
        if codici_str:
            ref_parts.append(f"Tit. {codici_str}")
        if data_str:
            ref_parts.append(data_str)

        desc = f"NC - {tratte_str}"
        if ref_parts:
            desc += f" ({', '.join(ref_parts)})"

        # Rif. fattura originale (se presente nell'XML)
        rif = self._extract_rif_fattura_originale(analysis)
        if rif:
            desc += f" rif.ft {rif}"

        return desc

    def _get_partner_payment_term(self, partner_id: int) -> Optional[int]:
        """
        Recupera il payment term del fornitore (property_supplier_payment_term_id).
        Ritorna l'id o None se non impostato.
        """
        try:
            partners = self.client._call('res.partner', 'search_read',
                [('id', '=', partner_id)],
                fields=['id', 'property_supplier_payment_term_id'])
            if partners:
                pt = partners[0].get('property_supplier_payment_term_id')
                if isinstance(pt, list) and pt:
                    return pt[0]
                if isinstance(pt, int):
                    return pt
        except Exception as e:
            logger.warning(f"Impossibile leggere payment_term per partner {partner_id}: {e}")
        return None

    def _top_conto_for_partner(self, partner_id: int,
                                 company_id: Optional[int] = None,
                                 months: int = 6,
                                 threshold: float = 0.8) -> Optional[int]:
        """
        Heuristica conto contabile dallo storico fornitore.

        Per un dato partner_id ricava il conto contabile più frequente
        nelle righe fattura/NC posted degli ultimi N mesi (filtrando le
        righe "tecniche" come IVA o debito tramite exclude_from_invoice_tab).

        Ritorna account_id solo se la frequenza del top conto >= threshold
        (default 80%): evita di applicare conto sbagliato per fornitori
        con pattern eterogeneo. Altrimenti None (caller deve usare fallback).

        Risultato cacheato per istanza per evitare query ripetute durante
        elaborazioni bulk.
        """
        if not partner_id:
            return None
        if not hasattr(self, '_top_conto_cache'):
            self._top_conto_cache = {}
        cache_key = (partner_id, company_id or 0, months, threshold)
        if cache_key in self._top_conto_cache:
            return self._top_conto_cache[cache_key]

        try:
            import datetime as _dt
            cutoff = (_dt.date.today() - _dt.timedelta(days=months * 30)).strftime('%Y-%m-%d')
            domain = [
                ('partner_id', '=', partner_id),
                ('parent_state', '=', 'posted'),
                ('move_id.move_type', 'in', ['in_invoice', 'in_refund']),
                ('exclude_from_invoice_tab', '=', False),
                ('date', '>=', cutoff),
            ]
            if company_id:
                domain.append(('company_id', '=', company_id))
            lines = self.client._call('account.move.line', 'search_read',
                domain, fields=['id', 'account_id'])

            if not lines:
                self._top_conto_cache[cache_key] = None
                return None

            from collections import Counter
            counter = Counter()
            for ln in lines:
                acc = ln.get('account_id')
                if isinstance(acc, list) and acc:
                    counter[acc[0]] += 1
            if not counter:
                self._top_conto_cache[cache_key] = None
                return None
            top_id, top_count = counter.most_common(1)[0]
            total = sum(counter.values())
            ratio = top_count / total
            if ratio >= threshold:
                self._top_conto_cache[cache_key] = top_id
                logger.info(f"Conto storico partner {partner_id}: account_id={top_id} "
                           f"({top_count}/{total} = {ratio*100:.0f}% >= {threshold*100:.0f}%)")
                return top_id
            else:
                self._top_conto_cache[cache_key] = None
                logger.info(f"Conto storico partner {partner_id}: top {top_id} "
                           f"solo {ratio*100:.0f}% < soglia, fallback")
                return None
        except Exception as e:
            logger.warning(f"Errore _top_conto_for_partner({partner_id}): {e}")
            self._top_conto_cache[cache_key] = None
            return None

    def _end_of_month(self, date_str: str) -> str:
        """
        Dato '2026-04-02' ritorna '2026-04-30' (ultimo giorno del mese).
        Usato per calcolare la Data Competenza IVA italiana.
        """
        if not date_str or '-' not in date_str:
            return date_str
        try:
            parts = date_str.split('-')
            y = int(parts[0])
            m = int(parts[1])
            last_day = calendar.monthrange(y, m)[1]
            return f"{y:04d}-{m:02d}-{last_day:02d}"
        except Exception as e:
            logger.warning(f"_end_of_month errore su '{date_str}': {e}")
            return date_str

    # === Azione principale: crea bozza fattura fornitore fisso === #

    def create_bozza_fornitore_fisso(self, analysis, mapping_entry: Dict) -> WriteResult:
        """
        Per una fattura classificata MAPPATURA_FORNITORE_FISSO:
        1. Trova 1 riga libera nell'OdA
        2. Aggiorna quella riga con prezzo+descrizione dalla fattura
        3. Crea account.move in draft
        4. Collega la riga alla move line
        5. Allega XML

        Se DRY_RUN, logga solo cosa farebbe e ritorna simulazione.
        """
        # Validazione mappatura
        err = self._validate_mapping(mapping_entry)
        if err:
            return WriteResult(success=False, action='create_draft',
                               error_message=err, dry_run=self.dry_run)

        # Validazione fattura
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        # Tipo documento: supporto TD01 (fatture), TD04 (note credito),
        # TD24 (fatture differite "ritiro al banco"), TD25 (fattura differita
        # art.21 c.4 ultimo periodo, casi triangolari) — entrambe trattate come TD01.
        # Gli altri tipi (TD05, TD06, ecc.) restano non supportati.
        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01', 'TD04', 'TD24', 'TD25'):
            return WriteResult(success=False, action='create_draft',
                               error_message=f"Tipo documento {tipo_doc} non supportato "
                                            f"(supportati: TD01/TD24/TD25 fatture, TD04 note credito)",
                               dry_run=self.dry_run)

        is_nota_credito = (tipo_doc == 'TD04')

        # Recupero OdA
        oda_name = mapping_entry['oda_fisso']
        po = self.client.search_purchase_order_by_name(oda_name)
        if not po:
            return WriteResult(success=False, action='create_draft',
                               error_message=f"OdA {oda_name} non trovato",
                               dry_run=self.dry_run)
        if po.get('state') != 'purchase':
            return WriteResult(success=False, action='create_draft',
                               error_message=f"OdA {oda_name} non in stato 'purchase' "
                                            f"(è '{po.get('state')}')",
                               dry_run=self.dry_run)

        # Cerco righe libere col criterio specificato in mappatura
        libere_criterio = mapping_entry.get('libere_criterio', 'standard_qty_inv_rec')
        libere = self._find_libere_purchase_order_lines(po['id'], libere_criterio)
        if not libere:
            return WriteResult(success=False, action='create_draft',
                               error_message=f"Nessuna riga libera in {oda_name} "
                                            f"(criterio: {libere_criterio}). "
                                            f"Aggiungere righe disponibili all'OdA.",
                               dry_run=self.dry_run)

        # Prendo la prima libera (già ordinate per prezzo crescente)
        po_line = libere[0]
        po_line_id = po_line['id']
        old_price = po_line.get('price_unit', 0)
        old_name = po_line.get('name', '')
        old_date_planned = po_line.get('date_planned') or None

        # Preparo i dati. Per NC il prezzo sulla riga OdA è negativo
        # (la riga si somma algebricamente al totale OdA e compensa la fattura
        # positiva originale). L'XML riporta sempre imponibile positivo.
        imponibile_xml = float(analysis.xml_data.imponibile_totale or 0)
        imponibile_oda_line = -imponibile_xml if is_nota_credito else imponibile_xml
        # Per il move_line delle NC: quantity=-1 e price_unit=-X così Odoo
        # con move_type=in_refund calcola PO.qty_invoiced = +1 (positivo).
        # Convenzione Ecotel confermata con contabilità (27-04-2026): la
        # colonna "Quantità Fatturata" sulla PO line deve essere positiva.
        # subtotal = (-1) * (-X) = +X (l'in_refund poi rigira contabilmente).
        imponibile_move = -imponibile_xml if is_nota_credito else imponibile_xml
        qty_move = -1 if is_nota_credito else 1

        # Descrizione: uso la strategia del fornitore (Trenitalia vs Italo)
        strategy = mapping_entry.get('description_strategy', 'trenitalia_titoli')
        description = self._build_description_by_strategy(
            analysis, strategy, is_nota_credito
        )
        # keep_original: usa la descrizione già presente nella riga OdA
        if description is None:
            description = old_name or ''

        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''

        # === DRY RUN: logga e ritorna ===
        if self.dry_run:
            tipo_str = "NC (in_refund)" if is_nota_credito else "FT (in_invoice)"
            logger.info(
                f"[DRY_RUN] create_bozza_fornitore_fisso [{tipo_str}]: "
                f"update PO line id={po_line_id}: price={imponibile_oda_line}, "
                f"name='{description}' | "
                f"create move partner={mapping_entry['partner_id']}, "
                f"ref={invoice_number}, date={invoice_date}, "
                f"imponibile={imponibile_move}, conto={mapping_entry['conto_contabile_id']}"
            )
            return WriteResult(
                success=True, action='create_draft',
                move_id=None, po_line_id=po_line_id,
                old_price_unit=old_price, old_name=old_name,
                old_date_planned=old_date_planned,
                dry_run=True,
            )

        # === SCRITTURA REALE ===
        try:
            # Step 1: Aggiorno la riga OdA "libera" con i dati della fattura.
            # Per NC: prezzo NEGATIVO (annulla la riga gemella della fattura
            # originale nel totale OdA). Per TD01: prezzo POSITIVO.
            # Scrivo anche qty_received=1 + qty_received_manual=1 per chiudere
            # il ciclo di ricevuta (lo fa l'operatore manualmente nelle righe
            # non-agent, dato che qty_received_method='manual' in questo OdA).
            # date_planned = data fattura (data consegna allineata).
            # IMPORTANTE: write in Odoo XML-RPC si chiama con (ids_list, vals_dict)
            # come DUE argomenti posizionali separati, non wrappati in una lista
            self.client._call('purchase.order.line', 'write',
                [po_line_id], {
                    'price_unit': imponibile_oda_line,
                    'name': description,
                    'product_qty': 1,
                    'qty_received': 1,
                    'qty_received_manual': 1,
                    'date_planned': invoice_date,
                })
            logger.info(f"Updated PO line {po_line_id}: price={imponibile_oda_line}, "
                       f"qty_received=1, date_planned={invoice_date}")

            # Step 2: Conto analitico: copio quello già valorizzato sulla
            # riga OdA libera. L'operatore lo predispone in anticipo quando
            # crea le righe libere nell'OdA.
            analytic_account_id = po_line.get('account_analytic_id')
            if isinstance(analytic_account_id, list):
                analytic_account_id = analytic_account_id[0]

            # Step 3: Payment term: recupero dal partner per calcolare la
            # scadenza corretta. Se il partner ha property_supplier_payment_term_id
            # lo propago al move, altrimenti Odoo userà il default.
            payment_term_id = self._get_partner_payment_term(
                mapping_entry['partner_id']
            )

            # Step 4: Creo account.move in draft
            # Per NC: move_type='in_refund' (nota credito fornitore).
            # Per TD01: move_type='in_invoice' (fattura fornitore).
            # In entrambi i casi importo nel move è POSITIVO. Il segno
            # contabile lo gestisce Odoo in base a move_type.
            move_line_vals = {
                'name': description,
                'quantity': qty_move,
                'price_unit': imponibile_move,
                'account_id': mapping_entry['conto_contabile_id'],
                'tax_ids': [(6, 0, mapping_entry['taxes_id'])],
                'purchase_line_id': po_line_id,
            }
            # product_id e product_uom_id: obbligatori via XML-RPC
            # (gli onchange non vengono triggerati, serve passarli esplicitamente)
            _prod = po_line.get('product_id')
            if isinstance(_prod, list):
                move_line_vals['product_id'] = _prod[0]
            elif _prod:
                move_line_vals['product_id'] = _prod
            _uom = po_line.get('product_uom')
            if isinstance(_uom, list):
                move_line_vals['product_uom_id'] = _uom[0]
            elif _uom:
                move_line_vals['product_uom_id'] = _uom
            if analytic_account_id:
                move_line_vals['analytic_account_id'] = analytic_account_id

            # Calcolo data competenza IVA = fine mese della data fattura.
            # Vale anche per NC (regola convenzionale italiana liquidazione IVA).
            data_competenza = self._end_of_month(invoice_date)

            move_type = 'in_refund' if is_nota_credito else 'in_invoice'
            move_vals = {
                'partner_id': mapping_entry['partner_id'],
                'move_type': move_type,
                'invoice_date': invoice_date,
                'date': data_competenza,
                'l10n_it_vat_settlement_date': data_competenza,
                'ref': invoice_number,
                'invoice_origin': oda_name,
                'journal_id': mapping_entry['journal_id'],
                'company_id': mapping_entry['company_id'],
                'invoice_line_ids': [(0, 0, move_line_vals)],
            }
            if payment_term_id:
                move_vals['invoice_payment_term_id'] = payment_term_id

            move_id = self.client._call('account.move', 'create', move_vals)
            # Normalizzo: se Odoo ritorna lista, estraggo il primo
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            logger.info(f"Created account.move id={move_id} [{move_type}]")

            # Step 4b: Collego il fatturapa.attachment.in al move e lo marco
            # come 'registered'. Questo fa sparire la fattura dal contenitore
            # "e-fatture in ingresso" e la collega ufficialmente al move.
            if analysis.attachment_id and move_id:
                try:
                    # Scrivo fatturapa_attachment_in_id sul move
                    self.client._call('account.move', 'write',
                        [move_id], {
                            'fatturapa_attachment_in_id': analysis.attachment_id,
                        })
                    # Marco l'attachment come registered=True
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {
                            'registered': True,
                        })
                    logger.info(f"Collegato fatturapa.attachment.in {analysis.attachment_id} "
                               f"al move {move_id} e marcato registered=True")
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa attachment fallito "
                                  f"(non blocca): {e}")

            # Step 5: Allego XML al move se presente
            if analysis.raw_xml and move_id:
                try:
                    attachment_vals = {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')
                        ).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    }
                    # create senza wrapping per evitare multi-create issues
                    self.client._call('ir.attachment', 'create', attachment_vals)
                    logger.info(f"XML allegato a move {move_id}")
                except Exception as e:
                    logger.warning(f"Allegato XML fallito (non blocca): {e}")

            return WriteResult(
                success=True, action='create_draft',
                move_id=move_id, po_line_id=po_line_id,
                old_price_unit=old_price, old_name=old_name,
                old_date_planned=old_date_planned,
                dry_run=False,
            )

        except Exception as e:
            logger.exception(f"Errore durante create_bozza_fornitore_fisso")
            # Tentativo di rollback parziale: se ho gia' aggiornato la riga,
            # ripristino tutti i campi (prezzo, descrizione, qty_received, date_planned)
            try:
                rbk_vals = {
                    'price_unit': old_price,
                    'name': old_name,
                    'qty_received': 0,
                    'qty_received_manual': 0,
                }
                if old_date_planned:
                    rbk_vals['date_planned'] = old_date_planned
                self.client._call('purchase.order.line', 'write',
                    [po_line_id], rbk_vals)
                logger.info(f"Rollback riga OdA {po_line_id} riuscito")
            except Exception as rbk_err:
                logger.error(f"Rollback riga OdA fallito: {rbk_err}")

            return WriteResult(
                success=False, action='create_draft',
                error_message=str(e),
                dry_run=False,
            )

    # === Multi-linea: crea bozza con N righe OdA + eventuale indennità === #

    def _separate_indennita_lines(self, xml_data):
        """
        Separa le righe XML in due gruppi:
        - main_lines: righe con aliquota IVA > 0 (servizi normali)
        - indennita_lines: righe con aliquota IVA == 0 e keyword indennità/interessi
        """
        main_lines = []
        indennita_lines = []
        for r in (xml_data.righe or []):
            desc = (r.descrizione or '').lower()
            is_indennita = (
                (r.aliquota_iva or 0) == 0
                and ('indennit' in desc or 'interess' in desc)
            )
            if is_indennita:
                indennita_lines.append(r)
            else:
                main_lines.append(r)
        return main_lines, indennita_lines

    def _match_lines_to_groups(self, main_lines, line_groups):
        """
        Per OdA multi-gruppo (es. P04516): assegna ogni riga XML a un gruppo
        in base alle keyword. Le righe non matchate vanno nel gruppo 'is_residual'.

        Ritorna dict: group_index -> [righe XML]
        """
        assignments = {i: [] for i in range(len(line_groups))}
        residual_idx = None
        for i, g in enumerate(line_groups):
            if g.get('is_residual'):
                residual_idx = i

        for r in main_lines:
            desc = (r.descrizione or '').lower()
            matched = False
            for i, g in enumerate(line_groups):
                if g.get('is_residual'):
                    continue
                if g['match'].lower() in desc:
                    assignments[i].append(r)
                    matched = True
                    break
            if not matched and residual_idx is not None:
                assignments[residual_idx].append(r)

        return assignments

    def _find_po_line_by_keyword(self, libere, keyword):
        """
        Tra le righe libere, trova la prima la cui descrizione contiene keyword.
        """
        kw = keyword.lower()
        for l in libere:
            if kw in (l.get('name', '') or '').lower():
                return l
        return None

    def _create_indennita_po_line(self, po_id, amount, description,
                                  indennita_config, template_po_line,
                                  invoice_date):
        """
        Crea una NUOVA riga OdA per l'indennità/interessi moratori.
        Usa template_po_line per copiare product_id, account_analytic_id, ecc.
        """
        product_id = indennita_config.get('product_id')
        if isinstance(template_po_line.get('product_id'), list):
            product_id = product_id or template_po_line['product_id'][0]

        product_uom = template_po_line.get('product_uom')
        if isinstance(product_uom, list):
            product_uom = product_uom[0]
        elif not product_uom:
            product_uom = 1  # Units

        analytic = template_po_line.get('account_analytic_id')
        if isinstance(analytic, list):
            analytic = analytic[0]

        vals = {
            'order_id': po_id,
            'name': description,
            'product_id': product_id,
            'product_qty': 1,
            'price_unit': amount,
            'qty_received': 1,
            'qty_received_manual': 1,
            'taxes_id': [(6, 0, indennita_config.get('taxes_id', [47]))],
            'date_planned': invoice_date,
        }
        if analytic:
            vals['account_analytic_id'] = analytic

        new_line_id = self.client._call('purchase.order.line', 'create', vals)
        if isinstance(new_line_id, list):
            new_line_id = new_line_id[0] if new_line_id else None
        logger.info(f"Created indennità PO line {new_line_id}: {amount}, "
                   f"desc='{description[:60]}'")
        return new_line_id

    def create_bozza_multilinea(self, analysis, mapping_entry: Dict) -> WriteResult:
        """
        Crea bozza per fornitori multi-linea (es. Telecom).
        Gestisce:
        - Singola riga OdA principale (default)
        - Multi-gruppo (line_groups): N righe OdA per fattura
        - Indennità/interessi: crea nuova riga OdA con tax diversa
        - Move con N move_lines

        Per rollback: po_line_id = prima riga consumata,
        extra_po_lines = le altre (inclusa eventuale indennità creata).
        """
        # Validazioni (come nel metodo originale)
        err = self._validate_mapping(mapping_entry)
        if err:
            return WriteResult(success=False, action='create_draft',
                               error_message=err, dry_run=self.dry_run)
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01', 'TD04', 'TD24', 'TD25'):
            return WriteResult(success=False, action='create_draft',
                               error_message=f"Tipo documento {tipo_doc} non supportato "
                                            f"(supportati: TD01/TD24/TD25 fatture, TD04 note credito)",
                               dry_run=self.dry_run)
        is_nota_credito = (tipo_doc == 'TD04')

        # Recupero OdA
        oda_name = mapping_entry['oda_fisso']
        po = self.client.search_purchase_order_by_name(oda_name)
        if not po:
            return WriteResult(success=False, action='create_draft',
                               error_message=f"OdA {oda_name} non trovato",
                               dry_run=self.dry_run)
        if po.get('state') != 'purchase':
            return WriteResult(success=False, action='create_draft',
                               error_message=f"OdA {oda_name} non in stato 'purchase'",
                               dry_run=self.dry_run)

        # Separo righe XML: principali vs indennità
        main_lines, indennita_lines = self._separate_indennita_lines(analysis.xml_data)

        # Recupero righe libere
        libere_criterio = mapping_entry.get('libere_criterio', 'standard_qty_inv_rec')
        libere = self._find_libere_purchase_order_lines(po['id'], libere_criterio)

        line_groups = mapping_entry.get('line_groups')
        indennita_config = mapping_entry.get('indennita_config')
        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        data_competenza = self._end_of_month(invoice_date)

        # === Costruisco le assegnazioni: quali righe XML vanno su quali PO line ===
        # Ogni entry: {'po_line': dict, 'amount': float, 'description': str,
        #              'taxes_id': [int], 'old_price': float, 'old_name': str,
        #              'old_date_planned': str, 'is_new': bool}
        assignments = []

        if line_groups:
            # Multi-gruppo: match righe XML ai gruppi, ogni gruppo → 1 PO line
            group_assignments = self._match_lines_to_groups(main_lines, line_groups)
            for i, group_cfg in enumerate(line_groups):
                group_lines = group_assignments.get(i, [])
                if not group_lines:
                    continue
                amount = sum(r.prezzo_totale for r in group_lines)
                if amount == 0:
                    continue
                # Trovo PO line libera il cui nome contiene il match keyword
                po_line = self._find_po_line_by_keyword(libere, group_cfg['match'])
                if not po_line:
                    return WriteResult(
                        success=False, action='create_draft',
                        error_message=f"Nessuna riga libera in {oda_name} "
                                     f"per gruppo '{group_cfg['match']}'",
                        dry_run=self.dry_run)
                # Rimuovo dalla lista libere per non riusarla
                libere = [l for l in libere if l['id'] != po_line['id']]
                assignments.append({
                    'po_line': po_line,
                    'amount': -amount if is_nota_credito else amount,
                    'move_amount': -amount if is_nota_credito else amount,
                    'description': po_line.get('name', ''),  # keep_original
                    'taxes_id': mapping_entry['taxes_id'],
                    'old_price': po_line.get('price_unit', 0),
                    'old_name': po_line.get('name', ''),
                    'old_date_planned': po_line.get('date_planned'),
                    'is_new': False,
                })
        elif mapping_entry.get('lines_one_to_one'):
            # 1 riga XML → 1 riga libera (in ordine). Adatto a OdA-ledger
            # mensili (es. Wind Tre Professional Full: fattura mensile con
            # 2 righe da €17,99 che consumano 2 righe libere consecutive).
            if len(libere) < len(main_lines):
                return WriteResult(
                    success=False, action='create_draft',
                    error_message=f"OdA {oda_name} ha {len(libere)} righe libere "
                                 f"ma la fattura ne richiede {len(main_lines)}",
                    dry_run=self.dry_run)
            for xml_line, po_line in zip(main_lines, libere):
                amount = xml_line.prezzo_totale
                if amount == 0:
                    continue
                assignments.append({
                    'po_line': po_line,
                    'amount': -amount if is_nota_credito else amount,
                    'move_amount': -amount if is_nota_credito else amount,
                    'description': po_line.get('name', ''),  # keep_original
                    'taxes_id': mapping_entry['taxes_id'],
                    'old_price': po_line.get('price_unit', 0),
                    'old_name': po_line.get('name', ''),
                    'old_date_planned': po_line.get('date_planned'),
                    'is_new': False,
                })
        else:
            # Singolo gruppo: tutto su 1 PO line
            if not libere:
                return WriteResult(
                    success=False, action='create_draft',
                    error_message=f"Nessuna riga libera in {oda_name}",
                    dry_run=self.dry_run)
            amount = sum(r.prezzo_totale for r in main_lines)
            po_line = libere[0]
            libere = libere[1:]
            assignments.append({
                'po_line': po_line,
                'amount': -amount if is_nota_credito else amount,
                'move_amount': amount,
                'description': po_line.get('name', ''),  # keep_original
                'taxes_id': mapping_entry['taxes_id'],
                'old_price': po_line.get('price_unit', 0),
                'old_name': po_line.get('name', ''),
                'old_date_planned': po_line.get('date_planned'),
                'is_new': False,
            })

        # Indennità: se ci sono righe interessi, creo una nuova PO line
        indennita_po_line_id = None
        if indennita_lines and indennita_config:
            indennita_amount = sum(r.prezzo_totale for r in indennita_lines)
            if indennita_amount != 0:
                # Costruisco descrizione sintetica
                descs = [r.descrizione.strip() for r in indennita_lines
                         if r.descrizione and r.descrizione.strip()]
                indennita_desc = ' + '.join(descs) if descs else 'Indennità e interessi'
                if invoice_number:
                    indennita_desc += f' rif.ft {invoice_number}'

                assignments.append({
                    'po_line': None,  # sarà creata
                    'amount': -indennita_amount if is_nota_credito else indennita_amount,
                    'move_amount': -indennita_amount if is_nota_credito else indennita_amount,
                    'description': indennita_desc,
                    'taxes_id': indennita_config['taxes_id'],
                    'old_price': 0,
                    'old_name': '',
                    'old_date_planned': None,
                    'is_new': True,  # da creare
                    'indennita_config': indennita_config,
                })

        if not assignments:
            return WriteResult(success=False, action='create_draft',
                               error_message="Nessuna riga da assegnare",
                               dry_run=self.dry_run)

        # Prima assegnazione come "principale" (per compatibilità WriteResult)
        primary = assignments[0]
        primary_po_line_id = primary['po_line']['id'] if primary['po_line'] else None

        # === DRY RUN ===
        if self.dry_run:
            tipo_str = "NC" if is_nota_credito else "FT"
            for a in assignments:
                pl_id = a['po_line']['id'] if a['po_line'] else 'NEW'
                logger.info(f"[DRY_RUN] multilinea [{tipo_str}]: "
                           f"PO line {pl_id}: amount={a['amount']}, "
                           f"desc='{a['description'][:50]}', taxes={a['taxes_id']}")
            return WriteResult(
                success=True, action='create_draft',
                move_id=None, po_line_id=primary_po_line_id,
                old_price_unit=primary['old_price'],
                old_name=primary['old_name'],
                old_date_planned=primary['old_date_planned'],
                dry_run=True,
            )

        # === SCRITTURA REALE ===
        updated_po_lines = []  # per rollback
        try:
            # Step 1: Aggiorno/creo tutte le righe OdA
            for a in assignments:
                if a['is_new']:
                    # Crea nuova riga OdA per indennità
                    template = assignments[0]['po_line']  # copia campi dal primo
                    new_id = self._create_indennita_po_line(
                        po['id'], a['amount'], a['description'],
                        a['indennita_config'], template, invoice_date)
                    a['po_line_id'] = new_id
                    # Leggo la riga appena creata per avere product_id/uom
                    new_pl = self.client._call('purchase.order.line', 'search_read',
                        [('id', '=', new_id)],
                        fields=['id', 'product_id', 'product_uom',
                                'account_analytic_id'])
                    a['po_line'] = new_pl[0] if new_pl else {}
                    updated_po_lines.append({
                        'po_line_id': new_id, 'is_new': True,
                        'old_price': 0, 'old_name': '',
                        'old_date_planned': None,
                    })
                else:
                    # Aggiorno riga esistente
                    pl = a['po_line']
                    pl_id = pl['id']
                    self.client._call('purchase.order.line', 'write',
                        [pl_id], {
                            'price_unit': a['amount'],
                            'name': a['description'],
                            'product_qty': 1,
                            'qty_received': 1,
                            'qty_received_manual': 1,
                            'date_planned': invoice_date,
                        })
                    a['po_line_id'] = pl_id
                    updated_po_lines.append({
                        'po_line_id': pl_id, 'is_new': False,
                        'old_price': a['old_price'],
                        'old_name': a['old_name'],
                        'old_date_planned': a['old_date_planned'],
                    })
                    logger.info(f"Updated PO line {pl_id}: price={a['amount']}")

            # Step 2: Payment term
            payment_term_id = self._get_partner_payment_term(
                mapping_entry['partner_id'])

            # Step 3: Costruisco move_line_ids (una per ogni assegnazione)
            move_line_ids = []
            for a in assignments:
                pl = a.get('po_line') or {}
                analytic = pl.get('account_analytic_id')
                if isinstance(analytic, list):
                    analytic = analytic[0]

                ml_vals = {
                    'name': a['description'],
                    # Per NC: quantity=-1 + price negativo → subtotal positivo;
                    # Odoo con in_refund calcola PO.qty_invoiced = +1 (positivo).
                    'quantity': -1 if is_nota_credito else 1,
                    'price_unit': a['move_amount'],
                    'account_id': mapping_entry['conto_contabile_id'],
                    'tax_ids': [(6, 0, a['taxes_id'])],
                    'purchase_line_id': a['po_line_id'],
                }
                # product_id e product_uom_id: obbligatori via XML-RPC
                # (gli onchange non vengono triggerati)
                prod = pl.get('product_id')
                if isinstance(prod, list):
                    ml_vals['product_id'] = prod[0]
                elif prod:
                    ml_vals['product_id'] = prod
                uom = pl.get('product_uom')
                if isinstance(uom, list):
                    ml_vals['product_uom_id'] = uom[0]
                elif uom:
                    ml_vals['product_uom_id'] = uom
                if analytic:
                    ml_vals['analytic_account_id'] = analytic
                move_line_ids.append((0, 0, ml_vals))

            # Step 4: Creo account.move
            move_type = 'in_refund' if is_nota_credito else 'in_invoice'
            move_vals = {
                'partner_id': mapping_entry['partner_id'],
                'move_type': move_type,
                'invoice_date': invoice_date,
                'date': data_competenza,
                'l10n_it_vat_settlement_date': data_competenza,
                'ref': invoice_number,
                'invoice_origin': oda_name,
                'journal_id': mapping_entry['journal_id'],
                'company_id': mapping_entry['company_id'],
                'invoice_line_ids': move_line_ids,
            }
            if payment_term_id:
                move_vals['invoice_payment_term_id'] = payment_term_id

            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            logger.info(f"Created account.move id={move_id} [{move_type}] "
                       f"with {len(move_line_ids)} lines")

            # Step 5: Collego fatturapa attachment
            if analysis.attachment_id and move_id:
                try:
                    self.client._call('account.move', 'write',
                        [move_id], {
                            'fatturapa_attachment_in_id': analysis.attachment_id,
                        })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                    logger.info(f"Collegato fatturapa attachment {analysis.attachment_id}")
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito: {e}")

            # Step 6: Allego XML
            if analysis.raw_xml and move_id:
                try:
                    self.client._call('ir.attachment', 'create', {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')
                        ).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    })
                except Exception as e:
                    logger.warning(f"Allegato XML fallito: {e}")

            # Preparo extra_po_lines per il risultato (escluso il primo)
            extra = []
            for upl in updated_po_lines[1:]:
                extra.append(upl)

            return WriteResult(
                success=True, action='create_draft',
                move_id=move_id,
                po_line_id=updated_po_lines[0]['po_line_id'],
                old_price_unit=updated_po_lines[0]['old_price'],
                old_name=updated_po_lines[0]['old_name'],
                old_date_planned=updated_po_lines[0]['old_date_planned'],
                extra_po_lines=extra if extra else None,
                dry_run=False,
            )

        except Exception as e:
            logger.exception("Errore durante create_bozza_multilinea")
            # Rollback: ripristino le righe aggiornate, cancello quelle create
            for upl in updated_po_lines:
                try:
                    if upl['is_new']:
                        self.client._call('purchase.order.line', 'unlink',
                            [upl['po_line_id']])
                        logger.info(f"Rollback: deleted new PO line {upl['po_line_id']}")
                    else:
                        rbk = {
                            'price_unit': upl['old_price'],
                            'name': upl['old_name'],
                            'qty_received': 0,
                            'qty_received_manual': 0,
                        }
                        if upl['old_date_planned']:
                            rbk['date_planned'] = upl['old_date_planned']
                        self.client._call('purchase.order.line', 'write',
                            [upl['po_line_id']], rbk)
                        logger.info(f"Rollback: restored PO line {upl['po_line_id']}")
                except Exception as rbk_err:
                    logger.error(f"Rollback PO line fallito: {rbk_err}")

            return WriteResult(
                success=False, action='create_draft',
                error_message=str(e), dry_run=False,
            )

    # === Writer per AUTO_VALIDABILE (replica "Crea fattura da OdA") === #

    def create_bozza_da_oda_matched(self, analysis) -> WriteResult:
        """
        Crea una bozza account.move replicando il flusso Odoo nativo
        "Crea fattura fornitore da OdA": le move_line vengono ricostruite
        dalle purchase.order.line del PO matchato (non dalle righe XML).

        Flusso:
        1. Recupera tutte le PO line del purchase_order
        2. Per ogni riga, calcola qty_to_invoice:
           - Merci (type=product/consu): qty_received - qty_invoiced
           - Servizi (type=service): product_qty - qty_invoiced
        3. Skip righe già completamente fatturate (qty_to_invoice <= 0)
        4. Per le merci non ricevute (qty_received < product_qty e residuo
           da fatturare > qty disponibile da ricezione): blocca con messaggio
        5. Verifica che totale move ≈ totale fattura XML (entro tolleranza)
        6. Crea account.move con tutte le move_line dedotte dalle PO line

        A differenza di create_bozza_fornitore_fisso:
        - NON richiede mapping in MAPPATURA_FORNITORI_FISSI
        - NON modifica le righe PO (solo collegamento via purchase_line_id)
        - Conto contabile dedotto da product.property_account_expense_id
          (con fallback a categoria) — controllo company-dependent
        - IVA dalla PO line
        - Per i prodotti senza conto configurato → errore esplicito

        Rollback: semplice unlink del move + de-registra attachment.
        """
        # --- Validazioni base ---
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01', 'TD04', 'TD24', 'TD25'):
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=f"Tipo {tipo_doc} non supportato "
                                            f"(solo TD01/TD24/TD25/TD04)",
                               dry_run=self.dry_run)
        is_nota_credito = (tipo_doc == 'TD04')

        po = analysis.purchase_order
        if not po:
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message="purchase_order mancante",
                               dry_run=self.dry_run)
        if po.get('state') != 'purchase':
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=f"OdA {po.get('name')} in stato "
                                            f"{po.get('state')!r}",
                               dry_run=self.dry_run)

        # --- Recupero TUTTE le PO line del PO matchato ---
        po_line_ids_all = po.get('order_line') or []
        if not po_line_ids_all:
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=f"OdA {po.get('name')} senza righe",
                               dry_run=self.dry_run)
        po_lines_data = self.client._call('purchase.order.line', 'read',
            po_line_ids_all,
            fields=['id', 'name', 'product_id', 'product_uom', 'taxes_id',
                    'qty_received', 'qty_received_manual', 'qty_invoiced',
                    'product_qty', 'price_unit', 'price_subtotal',
                    'account_analytic_id', 'sequence'])
        # Ordino per sequence per riprodurre l'ordine UI
        po_lines_data.sort(key=lambda l: (l.get('sequence') or 0, l['id']))

        # Per MATCH_DA_SUGGERIMENTO[_PIU_EXTRA] l'analyzer ha trovato un
        # SOTTOINSIEME di righe OdA che sommano l'imponibile fattura (al
        # centesimo, oppure con un piccolo extra per spese accessorie).
        # Filtro po_lines_data solo a quelle righe per non fatturare l'OdA pieno.
        suggested_pos = getattr(analysis, 'suggested_pos', None) or []
        is_subset_match = analysis.classification in (
            'MATCH_DA_SUGGERIMENTO', 'MATCH_DA_SUGGERIMENTO_PIU_EXTRA')
        extra_amount = 0.0
        if (is_subset_match and suggested_pos
                and suggested_pos[0].get('match_line_ids')):
            subset_ids = set(suggested_pos[0]['match_line_ids'])
            po_lines_data = [pl for pl in po_lines_data if pl['id'] in subset_ids]
            if not po_lines_data:
                return WriteResult(success=False, action='create_draft_from_oda',
                                   error_message=f"Subset MATCH_DA_SUGGERIMENTO "
                                                 f"{sorted(subset_ids)} non trovato "
                                                 f"tra le righe OdA",
                                   dry_run=self.dry_run)
            extra_amount = float(suggested_pos[0].get('extra_amount') or 0)

        # Recupero info dei prodotti (account, type) — passando il context
        # company_id perché property_account_expense_id è company-dependent
        company_id = po.get('company_id')
        if isinstance(company_id, list):
            company_id = company_id[0]
        ctx = {'force_company': company_id} if company_id else {}

        # Partner del PO: serve per l'heuristica conto storica (top conto
        # del fornitore). Calcolato qui perché viene usato durante la
        # costruzione di billable_lines.
        partner_id_po = po.get('partner_id')
        if isinstance(partner_id_po, list):
            partner_id_po = partner_id_po[0]

        product_ids = []
        for pl in po_lines_data:
            p = pl.get('product_id')
            if isinstance(p, list) and p:
                product_ids.append(p[0])
        product_ids = list(set(product_ids))
        products_by_id = {}
        if product_ids:
            prods = self.client._call('product.product', 'read', product_ids,
                fields=['id', 'name', 'type', 'categ_id',
                        'property_account_expense_id'],
                context=ctx)
            products_by_id = {p['id']: p for p in prods}

        # Conti delle categorie (fallback)
        categ_ids = list({c[0] for p in products_by_id.values()
                          for c in [p.get('categ_id') or []]
                          if isinstance(c, list) and c})
        categs_by_id = {}
        if categ_ids:
            cats = self.client._call('product.category', 'read', categ_ids,
                fields=['id', 'name', 'property_account_expense_categ_id'],
                context=ctx)
            categs_by_id = {c['id']: c for c in cats}

        # --- Per ogni PO line, calcolo quanto fatturare in questa bozza ---
        # Logica equivalente al wizard nativo "Crea fattura fornitore":
        # - merci (type=product/consu): qty_to_invoice = qty_received - qty_invoiced
        # - servizi (type=service): qty_to_invoice = product_qty - qty_invoiced
        # - già completamente fatturate: skip
        # - merci con qty_received < product_qty residuo: segnaliamo blocco
        billable_lines = []   # [(pl, qty_to_invoice, prod_data, account_id)]
        not_received = []     # righe merci non ricevute dal magazzino
        missing_account = []  # prodotti senza conto configurato

        for pl in po_lines_data:
            prod = pl.get('product_id')
            prod_id = prod[0] if isinstance(prod, list) else None
            prod_data = products_by_id.get(prod_id) if prod_id else None
            prod_type = (prod_data or {}).get('type') or 'service'

            qty_total = float(pl.get('product_qty') or 0)
            qty_inv = float(pl.get('qty_invoiced') or 0)
            qty_rec = float(pl.get('qty_received') or 0)

            if prod_type in ('product', 'consu'):
                qty_to_invoice = qty_rec - qty_inv
                qty_da_ricevere = qty_total - qty_rec
                # Blocco solo se c'è ancora qualcosa da fatturare e parte
                # è bloccata su ricezione (residuo non ancora ricevuto)
                if qty_to_invoice <= 0 and qty_da_ricevere > 0:
                    not_received.append({
                        'po_line_id': pl['id'],
                        'name': pl.get('name', '')[:80],
                        'qty_total': qty_total,
                        'qty_received': qty_rec,
                        'qty_invoiced': qty_inv,
                    })
                    continue
            else:
                qty_to_invoice = qty_total - qty_inv

            if qty_to_invoice <= 0.001:
                continue  # già fatturata o residuo trascurabile

            # Determino conto contabile in 3 step:
            # 1) Heuristica storica: conto top usato per questo fornitore
            #    nelle ultime fatture posted (>= 80% delle righe). È il più
            #    affidabile perché si basa su come la contabilità ha già
            #    classificato in passato.
            # 2) Conto specifico del prodotto (property_account_expense_id)
            # 3) Conto della categoria del prodotto (fallback finale)
            account_id = self._top_conto_for_partner(partner_id_po, company_id)
            if not account_id and prod_data:
                acc = prod_data.get('property_account_expense_id')
                if isinstance(acc, list) and acc:
                    account_id = acc[0]
                if not account_id:
                    cat = prod_data.get('categ_id')
                    cat_id = cat[0] if isinstance(cat, list) else None
                    cat_data = categs_by_id.get(cat_id) if cat_id else None
                    if cat_data:
                        acc2 = cat_data.get('property_account_expense_categ_id')
                        if isinstance(acc2, list) and acc2:
                            account_id = acc2[0]
            if not account_id:
                missing_account.append({
                    'po_line_id': pl['id'],
                    'name': pl.get('name', '')[:80],
                    'product': (prod[1] if isinstance(prod, list) and len(prod) > 1 else '?'),
                })
                continue

            billable_lines.append((pl, qty_to_invoice, prod_data, account_id))

        if missing_account:
            details = "; ".join(
                f"PO line {n['po_line_id']} prodotto '{n['product']}'"
                for n in missing_account[:5])
            return WriteResult(
                success=False, action='create_draft_from_oda',
                error_message=f"Conto contabile non configurato su {len(missing_account)} "
                             f"prodotti: {details}. Impostare property_account_expense_id "
                             f"sul prodotto/categoria in Odoo prima di registrare.",
                dry_run=self.dry_run,
            )

        if not_received:
            details = "; ".join(
                f"PO line {n['po_line_id']} '{n['name']}' "
                f"(qty totale {n['qty_total']:.2f}, ricevuto {n['qty_received']:.2f})"
                for n in not_received[:5])
            return WriteResult(
                success=False, action='create_draft_from_oda',
                error_message=f"Merce non ancora ricevuta dal magazzino su "
                             f"{len(not_received)} righe: {details}",
                dry_run=self.dry_run,
            )

        if not billable_lines:
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=f"OdA {po.get('name')}: nessuna riga "
                                            f"da fatturare (tutto già fatturato)",
                               dry_run=self.dry_run)

        # --- Verifica totale: somma billable_lines ≈ imponibile fattura XML ---
        sum_billable = sum(qty * float(pl.get('price_unit') or 0)
                          for pl, qty, _, _ in billable_lines)
        imponibile_xml = float(analysis.xml_data.imponibile_totale or 0)
        diff = abs(sum_billable - imponibile_xml)
        # Tolleranza: €0.50 assoluta, oppure 1% percentuale
        tolerance = max(0.5, abs(imponibile_xml) * 0.01)
        # Per MATCH_DA_SUGGERIMENTO_PIU_EXTRA accettiamo lo scostamento perché
        # creeremo una riga "spese accessorie" col delta sotto.
        if diff > tolerance and not (
                analysis.classification == 'MATCH_DA_SUGGERIMENTO_PIU_EXTRA'
                and is_subset_match):
            return WriteResult(
                success=False, action='create_draft_from_oda',
                error_message=(f"Discrepanza tra imponibile fattura (€{imponibile_xml:.2f}) "
                              f"e somma righe da fatturare nell'OdA (€{sum_billable:.2f}, "
                              f"diff €{sum_billable - imponibile_xml:+.2f}). "
                              f"Verificare a mano se è un caso di multi-fattura "
                              f"sullo stesso OdA o se manca qualcosa."),
                dry_run=self.dry_run,
            )

        # --- Costruzione move_line ---
        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        currency_id = po.get('currency_id')
        if isinstance(currency_id, list):
            currency_id = currency_id[0]
        partner_id = partner_id_po  # estratto sopra
        if not partner_id:
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message="partner_id non determinato dal PO",
                               dry_run=self.dry_run)

        # Journal acquisto della company
        journal_domain = [('type', '=', 'purchase')]
        if company_id:
            journal_domain.append(('company_id', '=', company_id))
        journals = self.client._call('account.journal', 'search_read',
            journal_domain, fields=['id', 'name'], limit=1)
        if not journals:
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=f"Nessun giornale di acquisto per company {company_id}",
                               dry_run=self.dry_run)
        journal_id = journals[0]['id']

        move_line_vals = []
        for pl, qty_to_invoice, prod_data, account_id in billable_lines:
            prod = pl.get('product_id')
            tax_ids = pl.get('taxes_id') or []
            price_unit = float(pl.get('price_unit') or 0)
            if is_nota_credito:
                # Per NC: quantity NEGATIVA + price_unit NEGATIVO → subtotal positivo.
                # Odoo con move_type=in_refund calcola PO.qty_invoiced=+1 (positivo).
                # Convenzione Ecotel (confermata 27-04-2026 con contabilità):
                # la colonna "Quantità Fatturata" sulla PO line deve essere positiva.
                price_unit = -abs(price_unit)
                qty_to_invoice = -abs(qty_to_invoice)

            descr = (pl.get('name') or '').strip()
            if is_nota_credito and not descr.upper().startswith('NC'):
                descr = f"NC - {descr}"

            ml_vals = {
                'name': descr,
                'quantity': qty_to_invoice,
                'price_unit': price_unit,
                'account_id': account_id,
                'tax_ids': [(6, 0, tax_ids)] if tax_ids else [(6, 0, [])],
                'purchase_line_id': pl['id'],
            }
            if isinstance(prod, list):
                ml_vals['product_id'] = prod[0]
            uom = pl.get('product_uom')
            if isinstance(uom, list):
                ml_vals['product_uom_id'] = uom[0]
            analytic = pl.get('account_analytic_id')
            if isinstance(analytic, list) and analytic:
                ml_vals['analytic_account_id'] = analytic[0]
            move_line_vals.append((0, 0, ml_vals))

        # Per MATCH_DA_SUGGERIMENTO_PIU_EXTRA: aggiungo riga "spese accessorie"
        # con il delta tra imponibile fattura e somma righe OdA matchate.
        # Account 420110 = costi di trasporto e spedizione (id=125 Ecotel).
        if (analysis.classification == 'MATCH_DA_SUGGERIMENTO_PIU_EXTRA'
                and abs(extra_amount) > 0.01):
            sgn = -1 if is_nota_credito else 1
            extra_qty = sgn * 1
            extra_price = sgn * abs(extra_amount)
            # IVA: prendo la prima dell'OdA matchato (probabilmente coerente).
            extra_tax_ids = []
            if billable_lines:
                first_pl = billable_lines[0][0]
                pl_taxes = first_pl.get('taxes_id') or []
                if pl_taxes:
                    extra_tax_ids = list(pl_taxes)
            extra_ml = {
                'name': 'Spese accessorie (trasporto/contributi)',
                'quantity': extra_qty,
                'price_unit': extra_price,
                'account_id': 125,  # 420110 costi di trasporto e spedizione
            }
            if extra_tax_ids:
                extra_ml['tax_ids'] = [(6, 0, extra_tax_ids)]
            move_line_vals.append((0, 0, extra_ml))
            logger.info(f"Aggiunta riga spese accessorie: EUR {extra_price:.2f}")

        # Payment term dal partner
        payment_term_id = self._get_partner_payment_term(partner_id)

        # Data competenza IVA = fine mese
        data_competenza = self._end_of_month(invoice_date)

        # Guard: l'agent opera SOLO su Ecotel (company_id=1).
        # Se il PO appartiene a un'altra company, blocco per evitare scritture
        # accidentali multi-company (e per evitare conflitti su partner condivisi).
        if not company_id:
            company_id = 1  # default Ecotel se mancante
        if company_id != 1:
            return WriteResult(
                success=False, action='create_draft_from_oda',
                error_message=f"OdA {po.get('name')} appartiene a company_id={company_id} "
                             f"(non Ecotel). L'agent registra solo su Ecotel.",
                dry_run=self.dry_run,
            )

        move_type = 'in_refund' if is_nota_credito else 'in_invoice'
        move_vals = {
            'partner_id': partner_id,
            'move_type': move_type,
            'invoice_date': invoice_date,
            'date': data_competenza,
            'l10n_it_vat_settlement_date': data_competenza,
            'ref': invoice_number,
            'invoice_origin': po.get('name'),
            'journal_id': journal_id,
            'company_id': company_id,
            'invoice_line_ids': move_line_vals,
        }
        if currency_id:
            move_vals['currency_id'] = currency_id
        if payment_term_id:
            move_vals['invoice_payment_term_id'] = payment_term_id

        primary_po_line_id = billable_lines[0][0]['id']

        # === DRY RUN ===
        if self.dry_run:
            tipo_str = "NC" if is_nota_credito else "FT"
            logger.info(f"[DRY_RUN] create_bozza_da_oda_matched [{tipo_str}] "
                       f"OdA {po.get('name')}: {len(move_line_vals)} righe, "
                       f"partner_id={partner_id}, journal={journal_id}, "
                       f"imponibile fattura €{imponibile_xml:.2f} ≈ somma righe €{sum_billable:.2f}")
            for tup in move_line_vals:
                _, _, ml = tup
                logger.info(f"  ML: pl_id={ml['purchase_line_id']} "
                           f"acc={ml['account_id']} qty={ml['quantity']:.2f} "
                           f"pu={ml['price_unit']:.2f} tax={ml['tax_ids']} "
                           f"name={ml['name'][:60]!r}")
            return WriteResult(
                success=True, action='create_draft_from_oda',
                move_id=None, po_line_id=primary_po_line_id,
                dry_run=True,
                extra_po_lines=[
                    {'po_line_id': pl['id']}
                    for pl, _, _, _ in billable_lines[1:]
                ] if len(billable_lines) > 1 else None,
            )

        # === SCRITTURA REALE ===
        try:
            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            logger.info(f"Created account.move id={move_id} [{move_type}] "
                       f"da OdA {po.get('name')}")

            # Per i servizi (type=service) aggiorno qty_received in modo cumulativo
            # sulla PO line (somma il delta fatturato a quanto già ricevuto manualmente).
            # Per le merci (product/consu) NON tocco qty_received: lo gestisce il magazzino.
            for pl, qty_to_invoice, prod_data, _account_id in billable_lines:
                ptype = (prod_data or {}).get('type') or 'service'
                if ptype not in ('service',):
                    continue
                delta = abs(qty_to_invoice)  # per NC qty_to_invoice è negativa, qui sommo abs
                if is_nota_credito:
                    # Per le NC sottrai dalla ricevuta (storno parziale del servizio)
                    delta = -delta
                old_rec_manual = float(pl.get('qty_received_manual') or 0)
                new_rec_manual = old_rec_manual + delta
                try:
                    self.client._call('purchase.order.line', 'write',
                        [pl['id']], {
                            'qty_received_manual': new_rec_manual,
                            'qty_received': new_rec_manual,
                        })
                    logger.info(f"PO line {pl['id']} (service): qty_received "
                               f"{old_rec_manual} -> {new_rec_manual}")
                except Exception as e:
                    logger.warning(f"Update qty_received PO line {pl['id']} fallito: {e}")

            # Collego fatturapa.attachment.in al move + marco registered=True
            # + creo ir.attachment con XML (stessa logica di create_bozza_fornitore_fisso)
            if analysis.attachment_id and move_id:
                try:
                    self.client._call('account.move', 'write',
                        [move_id], {
                            'fatturapa_attachment_in_id': analysis.attachment_id,
                        })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                    logger.info(f"Collegato fatturapa.attachment.in "
                               f"{analysis.attachment_id} al move {move_id}")
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito: {e}")

            # Allego XML come ir.attachment al move
            if analysis.raw_xml and move_id:
                try:
                    attachment_vals = {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')
                        ).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    }
                    self.client._call('ir.attachment', 'create', attachment_vals)
                    logger.info(f"XML allegato a move {move_id}")
                except Exception as e:
                    logger.warning(f"Allegato XML fallito (non blocca): {e}")

            return WriteResult(
                success=True, action='create_draft_from_oda',
                move_id=move_id,
                po_line_id=primary_po_line_id,
                dry_run=False,
                extra_po_lines=[
                    {'po_line_id': pl['id']}
                    for pl, _, _, _ in billable_lines[1:]
                ] if len(billable_lines) > 1 else None,
            )
        except Exception as e:
            logger.exception("Errore create_bozza_da_oda_matched")
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=str(e), dry_run=False)

    # === Rollback === #

    def rollback_bozza(self, move_id: int, po_line_id: Optional[int] = None,
                        old_price_unit: Optional[float] = None,
                        old_name: Optional[str] = None,
                        old_date_planned: Optional[str] = None,
                        attachment_id: Optional[int] = None) -> WriteResult:
        """
        Rollback di una bozza creata:
        1. Verifica che il move sia ancora in stato 'draft' (non cancella fatture posted!)
        2. Cancella il account.move
        3. Ripristina la purchase.order.line aggiornata al vecchio stato
           (prezzo, nome, data consegna, qty_received)
        4. Se attachment_id passato, lo de-registra (registered=False)
           per riportare la fattura nel contenitore "e-fatture in ingresso"

        Rifiuta rollback su fatture posted per sicurezza.
        """
        if self.dry_run:
            logger.info(f"[DRY_RUN] rollback_bozza move_id={move_id}, po_line={po_line_id}")
            return WriteResult(success=True, action='rollback',
                               move_id=move_id, po_line_id=po_line_id,
                               dry_run=True)

        try:
            # Verifico stato move
            moves = self.client._call('account.move', 'search_read',
                [('id', '=', move_id)],
                fields=['id', 'state', 'name'])
            if not moves:
                return WriteResult(success=False, action='rollback',
                                   error_message=f"Move {move_id} non trovato")
            move = moves[0]
            if move['state'] != 'draft':
                return WriteResult(success=False, action='rollback',
                                   error_message=f"Move {move_id} in stato "
                                                f"'{move['state']}' - rollback rifiutato "
                                                f"(solo bozze draft possono essere cancellate)")

            # Cancello il move
            self.client._call('account.move', 'unlink', [move_id])
            logger.info(f"Deleted account.move {move_id}")

            # De-registro l'attachment fatturapa (lo riporta nel contenitore
            # "e-fatture in ingresso")
            if attachment_id:
                try:
                    self.client._call('fatturapa.attachment.in', 'write',
                        [attachment_id], {'registered': False})
                    logger.info(f"De-registrato fatturapa.attachment.in {attachment_id}")
                except Exception as e:
                    logger.warning(f"De-registrazione attachment fallita (non blocca): {e}")

            # Ripristino la riga OdA se richiesto. Ripristino SEMPRE anche
            # qty_received=0 / qty_received_manual=0 per liberare la riga
            # completamente (anche se old values non lo specificano).
            if po_line_id:
                write_vals = {
                    'qty_received': 0,
                    'qty_received_manual': 0,
                }
                if old_price_unit is not None:
                    write_vals['price_unit'] = old_price_unit
                if old_name is not None:
                    write_vals['name'] = old_name
                if old_date_planned:
                    write_vals['date_planned'] = old_date_planned
                # write con 2 args posizionali: [ids] e dict vals
                self.client._call('purchase.order.line', 'write',
                    [po_line_id], write_vals)
                logger.info(f"Restored PO line {po_line_id}: "
                           f"price={old_price_unit}, name='{old_name}', "
                           f"date_planned={old_date_planned}, "
                           f"qty_received=0")

            return WriteResult(success=True, action='rollback',
                               move_id=move_id, po_line_id=po_line_id)

        except Exception as e:
            logger.exception(f"Errore durante rollback")
            return WriteResult(success=False, action='rollback',
                               error_message=str(e))

    # === Info === #

    def count_libere(self, po_name: str,
                      criterio: str = 'standard_qty_inv_rec') -> Optional[int]:
        """Numero di righe libere in un OdA per dashboard/UI."""
        po = self.client.search_purchase_order_by_name(po_name)
        if not po:
            return None
        libere = self._find_libere_purchase_order_lines(po['id'], criterio)
        return len(libere)

    def check_move_exists(self, move_id: int) -> bool:
        """
        Verifica se un account.move esiste ancora in Odoo.
        Usato dal polling per rilevare bozze cancellate manualmente.
        """
        try:
            count = self.client._call('account.move', 'search_count',
                [('id', '=', move_id)])
            return count > 0
        except Exception as e:
            logger.warning(f"Errore check move {move_id}: {e}")
            return True  # fail-safe: se non so, assumo esista

    def restore_po_line(self, po_line_id: int,
                        old_price_unit: Optional[float] = None,
                        old_name: Optional[str] = None,
                        old_date_planned: Optional[str] = None,
                        attachment_id: Optional[int] = None) -> WriteResult:
        """
        Ripristina una riga OdA al suo stato pre-agent (riga 'libera').
        Usato quando la bozza collegata è stata cancellata manualmente in Odoo.
        Se attachment_id passato, de-registra anche l'attachment fatturapa.
        """
        if self.dry_run:
            logger.info(f"[DRY_RUN] restore_po_line {po_line_id}")
            return WriteResult(success=True, action='restore_line',
                               po_line_id=po_line_id, dry_run=True)
        try:
            write_vals = {
                'qty_received': 0,
                'qty_received_manual': 0,
            }
            if old_price_unit is not None:
                write_vals['price_unit'] = old_price_unit
            else:
                write_vals['price_unit'] = 0
            if old_name is not None:
                write_vals['name'] = old_name
            else:
                write_vals['name'] = 'test agent'
            if old_date_planned:
                write_vals['date_planned'] = old_date_planned

            self.client._call('purchase.order.line', 'write',
                [po_line_id], write_vals)
            logger.info(f"Restored PO line {po_line_id}")

            # De-registro anche l'attachment fatturapa se passato
            if attachment_id:
                try:
                    self.client._call('fatturapa.attachment.in', 'write',
                        [attachment_id], {'registered': False})
                    logger.info(f"De-registrato fatturapa.attachment.in {attachment_id}")
                except Exception as e:
                    logger.warning(f"De-registrazione attachment fallita: {e}")

            return WriteResult(success=True, action='restore_line',
                               po_line_id=po_line_id,
                               old_price_unit=old_price_unit, old_name=old_name)
        except Exception as e:
            logger.exception(f"Errore restore_po_line")
            return WriteResult(success=False, action='restore_line',
                               po_line_id=po_line_id, error_message=str(e))

    # ============================================================
    # WRITER: bozza libera da XML (per DA_VERIFICARE con OdA univoco)
    # ============================================================

    # Cache per istanza writer: aliquota_xml -> tax_id
    _tax_cache_per_company: Dict = None  # type: ignore

    def _resolve_tax_for_aliquota(self, aliquota_xml: float,
                                  company_id: int = 1) -> Optional[int]:
        """
        Mappa l'aliquota IVA estratta dall'XML (es. 22.0) al corrispondente
        account.tax.id di Odoo (acquisti). Cache per istanza writer + company.

        Default IDs per Ecotel (verificati su istanza prod 30/04/2026):
        - 22.0 -> id=11 (22% S, standard servizi)
        - 10.0 -> primo tax 10% acquisti trovato
        -  4.0 -> primo tax 4% acquisti trovato
        -  0.0 -> primo tax 0% acquisti trovato (Esente/Non imponibile)

        Per altre aliquote: cerca dinamicamente in account.tax.

        Ritorna None se non trova nessuna tax compatibile.
        """
        if self._tax_cache_per_company is None:
            self._tax_cache_per_company = {}
        cache_key = (round(float(aliquota_xml or 0), 2), company_id)
        if cache_key in self._tax_cache_per_company:
            return self._tax_cache_per_company[cache_key]

        # Override hardcoded per Ecotel (validati)
        OVERRIDE = {
            (22.0, 1): 11,   # 22% S
        }
        if cache_key in OVERRIDE:
            tax_id = OVERRIDE[cache_key]
            self._tax_cache_per_company[cache_key] = tax_id
            return tax_id

        # Ricerca dinamica
        try:
            taxes = self.client._call('account.tax', 'search_read',
                [('type_tax_use', '=', 'purchase'),
                 ('amount', '=', float(aliquota_xml or 0)),
                 ('company_id', '=', company_id)],
                fields=['id', 'name', 'amount'],
                limit=5)
            if taxes:
                # Preferisco quelle con nome contenente "S" (standard) se presente
                preferred = [t for t in taxes if ' S' in (t.get('name') or '')]
                tax_id = (preferred or taxes)[0]['id']
                self._tax_cache_per_company[cache_key] = tax_id
                return tax_id
        except Exception as e:
            logger.warning(f"Tax lookup fallito per aliquota={aliquota_xml}: {e}")

        self._tax_cache_per_company[cache_key] = None
        return None

    def create_bozza_libera_da_xml(self, analysis) -> WriteResult:
        """
        Crea una bozza account.move ricostruita dalle righe dell'XML
        (NON dalle PO line dell'OdA), per casi DA_VERIFICARE con OdA univoco
        ma scostamento OdA-fattura non automatizzabile.

        Caratteristiche:
        - Una move_line per ogni DettaglioLinee dell'XML (1:1)
        - Importi presi dall'XML (price_unit dalla riga, quantity = 1
          oppure quantity XML se valorizzata)
        - account_id: heuristica top conto storico fornitore -> fallback
          conto della prima PO line OdA -> fallback 410100
        - tax_ids: mapping aliquota XML -> account.tax via _resolve_tax_for_aliquota
        - invoice_origin = nome OdA matchato (riferimento testuale)
        - NESSUNA modifica alle PO line: niente write su qty_received/qty_invoiced.
          La connessione contabile-OdA si fa manualmente in Odoo dal contabile.
        - Per NC TD04: convenzione qty=-1, price=-X (subtotal positivo, come da
          memoria project_nc_convention validata in prod)
        - Allegato XML al move + collegamento fatturapa.attachment.in

        Args:
            analysis: InvoiceAnalysis con xml_data, purchase_order, attachment_id

        Returns:
            WriteResult con action='create_draft_libera'
        """
        # === Validazioni base ===
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01', 'TD04', 'TD24', 'TD25'):
            return WriteResult(success=False, action='create_draft_libera',
                               error_message=f"Tipo {tipo_doc} non supportato "
                                            f"(solo TD01/TD24/TD25/TD04)",
                               dry_run=self.dry_run)
        is_nota_credito = (tipo_doc == 'TD04')

        po = analysis.purchase_order
        if not po:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message="purchase_order mancante: "
                                            "questo writer richiede OdA univoco",
                               dry_run=self.dry_run)

        partner_id = getattr(analysis, '_partner_id_odoo', None)
        if not partner_id:
            partner_id = po.get('partner_id')
            if isinstance(partner_id, list):
                partner_id = partner_id[0]
        if not partner_id:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message="partner_id non determinabile",
                               dry_run=self.dry_run)

        company_id = po.get('company_id')
        if isinstance(company_id, list):
            company_id = company_id[0]
        if not company_id:
            company_id = 1
        # Guard "solo Ecotel"
        if company_id != 1:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message=f"OdA su company_id={company_id} "
                                            f"diversa da Ecotel (1) - bloccato",
                               dry_run=self.dry_run)

        # Righe XML
        righe_xml = list(analysis.xml_data.righe or [])
        if not righe_xml:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message="XML senza righe",
                               dry_run=self.dry_run)

        # === Heuristica conto contabile ===
        account_id_default = self._top_conto_for_partner(
            partner_id, company_id=company_id, months=6, threshold=0.8)
        # Fallback: conto della prima PO line dell'OdA
        if not account_id_default:
            try:
                po_line_ids_all = po.get('order_line') or []
                if po_line_ids_all:
                    pls = self.client._call('purchase.order.line', 'read',
                        po_line_ids_all[:3], fields=['product_id'])
                    for pl in pls:
                        prod = pl.get('product_id')
                        if isinstance(prod, list) and prod:
                            prod_data = self.client._call('product.product', 'read',
                                [prod[0]], fields=['property_account_expense_id',
                                                   'categ_id'])
                            if prod_data:
                                p = prod_data[0]
                                acc = p.get('property_account_expense_id')
                                if isinstance(acc, list) and acc:
                                    account_id_default = acc[0]
                                    break
                                # fallback categ
                                cat = p.get('categ_id')
                                if isinstance(cat, list) and cat:
                                    cat_data = self.client._call(
                                        'product.category', 'read', [cat[0]],
                                        fields=['property_account_expense_categ_id'])
                                    if cat_data:
                                        ca = cat_data[0].get(
                                            'property_account_expense_categ_id')
                                        if isinstance(ca, list) and ca:
                                            account_id_default = ca[0]
                                            break
            except Exception as e:
                logger.warning(f"Heuristica conto da PO fallita: {e}")

        # Fallback finale: conto generico merci 410100 (id=124 in Ecotel,
        # ma se non lo trovo, lascio None e Odoo solleverà errore visibile)
        if not account_id_default:
            try:
                acc = self.client._call('account.account', 'search_read',
                    [('code', '=', '410100'), ('company_id', '=', company_id)],
                    fields=['id'], limit=1)
                if acc:
                    account_id_default = acc[0]['id']
            except Exception:
                pass

        # === Date e header move ===
        invoice_date = analysis.xml_data.data
        if not invoice_date:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message="data fattura mancante",
                               dry_run=self.dry_run)
        data_competenza = self._end_of_month(invoice_date)
        invoice_number = analysis.xml_data.numero or ''
        oda_name = po.get('name') or ''

        # Currency dal PO
        currency_id = po.get('currency_id')
        if isinstance(currency_id, list):
            currency_id = currency_id[0]

        # Journal: cerco journal di acquisto dell'azienda
        journal_id = None
        try:
            jrns = self.client._call('account.journal', 'search_read',
                [('type', '=', 'purchase'), ('company_id', '=', company_id)],
                fields=['id'], limit=1)
            if jrns:
                journal_id = jrns[0]['id']
        except Exception:
            pass
        if not journal_id:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message="Journal acquisti non trovato "
                                            f"per company {company_id}",
                               dry_run=self.dry_run)

        # === Costruzione move_line per ogni riga XML ===
        sgn = -1 if is_nota_credito else 1
        invoice_line_ids = []
        for r in righe_xml:
            desc = (r.descrizione or '').strip()[:200] or f"Riga {r.numero_linea}"
            # Per NC: prefix
            if is_nota_credito:
                desc = f"NC - {desc}"
            qty_xml = float(r.quantita or 0)
            price_xml = float(r.prezzo_totale or 0)
            # Se quantita XML <= 0 o non valorizzata, uso 1 e metto tutto
            # in price_unit. Convenzione NC: qty=-1, price=-X.
            if qty_xml > 0 and price_xml > 0:
                # Quantity originale, price_unit = unitario
                quantity_move = sgn * qty_xml
                price_unit = sgn * (price_xml / qty_xml)
            else:
                # Aggregazione su 1 riga
                quantity_move = sgn * 1
                price_unit = sgn * abs(price_xml)
            tax_id = self._resolve_tax_for_aliquota(
                r.aliquota_iva or 0, company_id=company_id)
            line_vals = {
                'name': desc,
                'quantity': quantity_move,
                'price_unit': price_unit,
                'account_id': account_id_default,
            }
            if tax_id:
                line_vals['tax_ids'] = [(6, 0, [tax_id])]
            invoice_line_ids.append((0, 0, line_vals))

        if not invoice_line_ids:
            return WriteResult(success=False, action='create_draft_libera',
                               error_message="Nessuna move_line generata",
                               dry_run=self.dry_run)

        # Payment term dal partner
        payment_term_id = self._get_partner_payment_term(partner_id)

        move_type = 'in_refund' if is_nota_credito else 'in_invoice'
        move_vals = {
            'partner_id': partner_id,
            'move_type': move_type,
            'invoice_date': invoice_date,
            'date': data_competenza,
            'l10n_it_vat_settlement_date': data_competenza,
            'ref': invoice_number,
            'invoice_origin': oda_name,
            'journal_id': journal_id,
            'company_id': company_id,
            'invoice_line_ids': invoice_line_ids,
        }
        if currency_id:
            move_vals['currency_id'] = currency_id
        if payment_term_id:
            move_vals['invoice_payment_term_id'] = payment_term_id

        # === DRY RUN ===
        if self.dry_run:
            logger.info(f"[DRY_RUN] Bozza libera per {oda_name}: {len(invoice_line_ids)} righe, "
                       f"conto={account_id_default}, totale_ind={analysis.xml_data.imponibile_totale}")
            return WriteResult(success=True, action='create_draft_libera',
                               dry_run=True)

        # === SCRITTURA REALE ===
        try:
            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            logger.info(f"Created bozza libera move id={move_id} [{move_type}] "
                       f"da {len(invoice_line_ids)} righe XML")

            # Collegamento fatturapa
            if analysis.attachment_id and move_id:
                try:
                    self.client._call('account.move', 'write',
                        [move_id], {
                            'fatturapa_attachment_in_id': analysis.attachment_id,
                        })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                    logger.info(f"Collegato fatturapa.attachment.in "
                               f"{analysis.attachment_id} al move {move_id}")
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito: {e}")

            # Allego XML al move
            if analysis.raw_xml and move_id:
                try:
                    attachment_vals = {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')
                        ).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    }
                    self.client._call('ir.attachment', 'create', attachment_vals)
                    logger.info(f"XML allegato a move {move_id}")
                except Exception as e:
                    logger.warning(f"Allegato XML fallito: {e}")

            return WriteResult(
                success=True, action='create_draft_libera',
                move_id=move_id, dry_run=False,
            )
        except Exception as e:
            logger.exception(f"Errore create_bozza_libera_da_xml")
            return WriteResult(
                success=False, action='create_draft_libera',
                error_message=str(e), dry_run=False,
            )
