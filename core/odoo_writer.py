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
import re
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
    # POL aggiunte all'OdA per gestire righe accessorie (trasporto/bolli/...)
    # Necessario al rollback per de-creare anche queste righe quando si elimina
    # il move. Lista di int (purchase.order.line.id).
    added_po_line_ids: Optional[List[int]] = None

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
        if self.added_po_line_ids:
            d['added_po_line_ids'] = self.added_po_line_ids
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

    def _find_pol_autostrade_match(self, po_id: int, cc: str,
                                      classificazione: str,
                                      excluded_ids: Optional[set] = None) -> List[Dict]:
        """Cerca POL libere su un OdA Autostrade che match (cc + cls).

        Pattern descrizione P03718: "PEDAGGI AUTOSTRADALI\\nCodice cliente:
        NNN\\n<furgoni|uso promiscuo>".

        Strategia priorità:
        1. POL cc-specifica (descrizione contiene SOLO il cc richiesto)
        2. POL jolly (descrizione contiene il cc richiesto + altri cc)

        Filtri base: qty_invoiced=0 AND qty_received=0 AND product_qty>=1.

        excluded_ids: insieme di POL già "prenotate" da altre fatture nello
        stesso batch (per evitare di consumare 2 volte la stessa riga).

        Ritorna lista di POL (cc-specific prima, poi jolly), ordinata per id.
        """
        excluded = excluded_ids or set()
        cls_token = classificazione.replace('_', ' ')  # uso_promiscuo -> uso promiscuo
        lines = self.client._call('purchase.order.line', 'search_read',
            [('order_id', '=', po_id)],
            fields=['id', 'name', 'product_id', 'product_qty',
                     'price_unit', 'qty_invoiced', 'qty_received',
                     'taxes_id', 'account_analytic_id', 'date_planned',
                     'product_uom'])

        cc_specific = []
        jolly = []
        for ln in lines:
            if ln['id'] in excluded:
                continue
            if ((ln.get('qty_invoiced') or 0) != 0
                or (ln.get('qty_received') or 0) != 0
                or (ln.get('product_qty') or 0) < 1):
                continue
            desc = (ln.get('name') or '').lower()
            if cc not in desc or cls_token not in desc:
                continue
            # Conto quanti cc distinti compaiono nella descrizione (jolly se
            # >1, cc-specific se solo 1).
            ccs_in_desc = re.findall(r'\b\d{8,12}\b', ln.get('name') or '')
            unique_ccs = set(ccs_in_desc)
            if len(unique_ccs) <= 1:
                cc_specific.append(ln)
            else:
                jolly.append(ln)

        cc_specific.sort(key=lambda l: l['id'])
        jolly.sort(key=lambda l: l['id'])
        return cc_specific + jolly

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
        Usato come fallback per la data contabile quando manca la data SdI.
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

    def _data_contabile(self, analysis, invoice_date: str) -> str:
        """
        Convenzione Ecotel (decisa 2026-05-04 con contabilità):
        la data contabile (= data competenza IVA, opzione A) coincide con la
        DATA DI RICEZIONE SdI dell'allegato, cioè il `create_date` del record
        `fatturapa.attachment.in` su Odoo, troncato a 'YYYY-MM-DD'.

        Fallback: se per qualche motivo `attachment_create_date` non è
        disponibile (es. analisi vecchia ricaricata da DB, import manuale
        senza propagazione), torno alla logica precedente fine mese della
        data fattura.
        """
        cd = getattr(analysis, 'attachment_create_date', '') or ''
        if cd:
            # Odoo serializza datetime come 'YYYY-MM-DD HH:MM:SS'. Tronco.
            cd = cd.strip()
            date_part = cd.split(' ')[0] if ' ' in cd else cd[:10]
            if len(date_part) >= 10 and date_part[4] == '-' and date_part[7] == '-':
                return date_part
            logger.warning(f"_data_contabile: create_date inatteso '{cd}', "
                           f"fallback a fine mese di '{invoice_date}'")
        return self._end_of_month(invoice_date)

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
            data_contabile = self._data_contabile(analysis, invoice_date)
            data_competenza_iva = self._end_of_month(invoice_date)

            move_type = 'in_refund' if is_nota_credito else 'in_invoice'
            move_vals = {
                'partner_id': mapping_entry['partner_id'],
                'move_type': move_type,
                'invoice_date': invoice_date,
                'date': data_contabile,
                'l10n_it_vat_settlement_date': data_competenza_iva,
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

    def _classify_extra_line(self, descrizione: str) -> str:
        """Classifica una riga extra della fattura via keyword.
        Ritorna la categoria (TRASPORTO/BOLLO/ONERI_BANCARI/...) o '_DEFAULT'."""
        from config.rules import KEYWORD_RULES
        desc_lower = (descrizione or '').lower()
        for kw, _conto_key, categoria in KEYWORD_RULES:
            if kw in desc_lower:
                return categoria
        return '_DEFAULT'

    def _add_extra_pol_to_oda(self, po_id, extra_line_xml, template_po_line,
                              invoice_date) -> Optional[Dict]:
        """
        Crea una purchase.order.line dedicata sull'OdA per una riga extra
        (accessoria) della fattura: trasporto, bollo, oneri bancari, ecc.

        Pattern operatore (audit storico 90 giorni): il 100% delle righe
        accessorie nelle fatture posted è collegato a una POL specifica
        dell'OdA — l'addetto integra l'OdA con una riga dedicata e poi
        registra il move che si linka regolarmente. Questo helper emula
        esattamente quel comportamento.

        Args:
          po_id: ID purchase.order esistente (state=purchase, to invoice)
          extra_line_xml: dict-like con keys 'descrizione', 'prezzo_totale',
                          'aliquota_iva', 'quantita'
          template_po_line: una POL esistente del PO da cui ereditare
                            product_uom e account_analytic_id (consistenza
                            commessa analitica con le altre righe merce)
          invoice_date: stringa YYYY-MM-DD per date_planned

        Returns:
          dict con i dati della POL appena creata, formato compatibile con
          po_lines_data (id, name, product_id, product_qty, product_uom,
          qty_received, qty_invoiced, price_unit, price_subtotal, taxes_id,
          account_analytic_id) + campo extra '_account_id_override' che il
          caller usa per forzare l'account_id sulla move_line.
          In dry-run, 'id' = None (POL non realmente creata).
          Ritorna None se la riga è inammissibile (importo 0, oltre soglia).
        """
        from config.rules import (EXTRA_POL_MAPPING_ECOTEL,
                                   EXTRA_POL_TAX_BY_IVA_ECOTEL,
                                   ADD_EXTRA_POL_MAX_AMOUNT)

        # Estrazione dati riga (gestisce sia dict che dataclass FatturaPALine)
        if hasattr(extra_line_xml, 'descrizione'):
            desc = (extra_line_xml.descrizione or '').strip()
            amount = float(extra_line_xml.prezzo_totale or 0)
            aliquota = float(extra_line_xml.aliquota_iva or 22.0)
            quantita = float(extra_line_xml.quantita or 1)
        else:
            desc = (extra_line_xml.get('descrizione') or '').strip()
            # Fallback su 'prezzo' per retrocompat (analyzer legacy)
            amount = float(extra_line_xml.get('prezzo_totale')
                          or extra_line_xml.get('prezzo') or 0)
            aliquota = float(extra_line_xml.get('aliquota_iva') or 22.0)
            quantita = float(extra_line_xml.get('quantita') or 1)

        if amount <= 0:
            return None
        if amount > ADD_EXTRA_POL_MAX_AMOUNT:
            logger.warning(
                f"Extra POL skipped: amount EUR {amount:.2f} > soglia "
                f"EUR {ADD_EXTRA_POL_MAX_AMOUNT:.2f} ('{desc[:40]}')")
            return None
        if quantita <= 0:
            quantita = 1.0

        # 1. Classifica via keyword
        categoria = self._classify_extra_line(desc)
        mapping = EXTRA_POL_MAPPING_ECOTEL.get(
            categoria, EXTRA_POL_MAPPING_ECOTEL['_DEFAULT'])
        product_id = mapping['product_id']
        account_id_override = mapping['account_id']

        # 2. Tax id da aliquota IVA della riga XML
        tax_id = EXTRA_POL_TAX_BY_IVA_ECOTEL.get(aliquota)
        if tax_id is None:
            # Fallback prudente: 22% G (caso più comune Ecotel)
            tax_id = EXTRA_POL_TAX_BY_IVA_ECOTEL.get(22.0)
            logger.warning(f"Extra POL: aliquota {aliquota}% non mappata, "
                          f"fallback 22% G (tax_id={tax_id})")
        tax_ids_list = [tax_id] if tax_id else []

        # 3. UoM dalla POL template (default Units=1)
        product_uom = template_po_line.get('product_uom') if template_po_line else None
        if isinstance(product_uom, list):
            product_uom = product_uom[0]
        elif not product_uom:
            product_uom = 1

        # 4. Analitico dalla POL template (per coerenza commessa)
        analytic = None
        if template_po_line:
            a = template_po_line.get('account_analytic_id')
            if isinstance(a, list) and a:
                analytic = a[0]

        # 5. Calcolo price_unit; subtotal stimato = amount (entra nei controlli totale)
        price_unit = amount / quantita if quantita > 0 else amount

        # 5b. PRIMA di creare una POL nuova, cerca sull'OdA una POL libera
        # coerente (stesso product accessorio, qty_invoiced=0, price_subtotal
        # ~ amount, non gia' annullata). Replica il pattern operatore: se la
        # riga e' gia' lì pronta, la consumiamo invece di crearne una nuova.
        # Evita il bug "POL trasporto duplicata" (RemaTarlazzi V6/2026/41121
        # e altri): senza questo passaggio _add_extra_pol_to_oda creava sempre
        # una POL nuova e raddoppiava il billing rispetto alla POL libera
        # esistente, facendo fallire il check totale con "Discrepanza".
        try:
            tol_abs = max(0.01, abs(amount) * 0.01)
            candidates = self.client._call(
                'purchase.order.line', 'search_read',
                [('order_id', '=', po_id),
                 ('product_id', '=', product_id),
                 ('qty_invoiced', '=', 0)],
                fields=['id', 'name', 'product_id', 'product_qty',
                        'product_uom', 'price_unit', 'price_subtotal',
                        'discount', 'qty_received', 'qty_received_manual',
                        'qty_invoiced', 'taxes_id', 'account_analytic_id',
                        'sequence'])
            reused = None
            for c in candidates:
                cname = (c.get('name') or '').strip()
                if cname.upper().startswith('[ANNULLATA]'):
                    continue
                csub = float(c.get('price_subtotal') or 0)
                if abs(csub - amount) <= tol_abs:
                    reused = c
                    break
            if reused is not None:
                logger.info(
                    f"Riuso POL libera esistente id={reused['id']} "
                    f"cat={categoria} po_id={po_id} desc='{desc[:60]}' "
                    f"amount=EUR {amount:.2f} (price_subtotal POL "
                    f"EUR {float(reused.get('price_subtotal') or 0):.2f})")
                reused['_account_id_override'] = account_id_override
                reused['_extra_categoria'] = categoria
                reused['_reused_existing'] = True
                return reused
        except Exception as e:
            # Non bloccante: se la search fallisce per qualsiasi motivo,
            # ricado nel comportamento legacy (creo POL nuova).
            logger.warning(f"Lookup POL libera fallito (procedo a create): {e}")

        vals = {
            'order_id': po_id,
            'name': desc,
            'product_id': product_id,
            'product_qty': quantita,
            'product_uom': product_uom,
            'price_unit': price_unit,
            # qty_received NON impostato qui: il writer principale
            # (in create_bozza_da_oda_matched) farà il ciclo finale di
            # aggiornamento qty_received_manual sommando il delta fatturato.
            # Se settassimo qui qty_received=quantita, il delta verrebbe
            # sommato 2 volte → quantità ricevuta = 2 sull'OdA (bug).
            'taxes_id': [(6, 0, tax_ids_list)],
            'date_planned': invoice_date,
        }
        if analytic:
            vals['account_analytic_id'] = analytic

        # 6. Crea (o simula in dry-run)
        if self.dry_run:
            logger.info(
                f"[DRY_RUN] Simulato create extra POL cat={categoria} "
                f"po_id={po_id} desc='{desc[:60]}' amount=EUR {amount:.2f}")
            new_pol_id = None
        else:
            new_pol_id = self.client._call(
                'purchase.order.line', 'create', vals)
            if isinstance(new_pol_id, list):
                new_pol_id = new_pol_id[0] if new_pol_id else None
            logger.info(
                f"Created extra POL id={new_pol_id} cat={categoria} "
                f"po_id={po_id} desc='{desc[:60]}' amount=EUR {amount:.2f}")

        # Ritorno dict compatibile con po_lines_data.
        # qty_received=0 e qty_received_manual=0 perché il ciclo finale del
        # writer somma il delta fatturato (delta=quantita) ai valori esistenti
        # → risultato finale qty_received=quantita corretto.
        return {
            'id': new_pol_id,
            'name': desc,
            'product_id': [product_id, ''],   # formato Odoo Many2one
            'product_qty': quantita,
            'product_uom': [product_uom, ''],
            'qty_received': 0.0,
            'qty_received_manual': 0.0,
            'qty_invoiced': 0.0,
            'price_unit': price_unit,
            'price_subtotal': round(price_unit * quantita, 2),
            'taxes_id': tax_ids_list,
            'account_analytic_id': [analytic, ''] if analytic else False,
            '_extra_categoria': categoria,
            '_account_id_override': account_id_override,
        }

    def _cleanup_extra_pols(self, added_po_line_ids):
        """
        Best-effort cleanup delle POL extra create da _add_extra_pol_to_oda
        quando la creazione bozza fallisce DOPO l'aggiunta (es. discrepanza
        imponibile, missing_account, not_received).

        Strategia: prima tento unlink (passa solo se OdA in stato 'draft');
        se fallisce per stato 'purchase', azzero la riga (qty=0, price=0,
        prefisso "[ANNULLATA]" nel nome). La POL non sparisce ma non incide
        sui totali OdA. Mai solleva eccezione: il fail di cleanup non deve
        bloccare il fail principale che si sta riportando al chiamante.
        """
        if not added_po_line_ids:
            return
        if self.dry_run:
            logger.info(f"[DRY_RUN] cleanup POL extra simulato: {added_po_line_ids}")
            return
        try:
            existing = self.client._call(
                'purchase.order.line', 'search_read',
                [('id', 'in', list(added_po_line_ids))],
                fields=['id', 'name'])
        except Exception as e:
            logger.warning(f"Cleanup POL extra: lettura fallita "
                           f"{added_po_line_ids}: {e}")
            return
        for pol in existing:
            pol_id = pol['id']
            orig_name = pol.get('name', '') or ''
            try:
                self.client._call('purchase.order.line', 'unlink', [pol_id])
                logger.warning(f"Cleanup: rimossa POL extra id={pol_id} "
                               f"dopo fail bozza")
            except Exception:
                try:
                    new_name = orig_name
                    if not new_name.startswith('[ANNULLATA]'):
                        new_name = f"[ANNULLATA] {orig_name}"
                    self.client._call('purchase.order.line', 'write',
                        [pol_id], {
                            'product_qty': 0,
                            'price_unit': 0,
                            'qty_received': 0,
                            'qty_received_manual': 0,
                            'name': new_name,
                        })
                    logger.warning(f"Cleanup: azzerata POL extra id={pol_id} "
                                   f"(qty=0, price=0, prefix [ANNULLATA])")
                except Exception as e2:
                    logger.error(f"Cleanup POL extra {pol_id} fallito "
                                 f"completamente (POL fantasma sull'OdA): {e2}")

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
        data_contabile = self._data_contabile(analysis, invoice_date)
        data_competenza_iva = self._end_of_month(invoice_date)

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
                'date': data_contabile,
                'l10n_it_vat_settlement_date': data_competenza_iva,
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
                    'product_qty', 'price_unit', 'price_subtotal', 'discount',
                    'account_analytic_id', 'sequence'])
        # Ordino per sequence per riprodurre l'ordine UI
        po_lines_data.sort(key=lambda l: (l.get('sequence') or 0, l['id']))

        # Subset filtering — due percorsi possibili, entrambi consumano solo
        # un sottoinsieme delle righe OdA (non l'intero ordine):
        #   1) MATCH_DA_SUGGERIMENTO[_PIU_EXTRA]: line_ids da analysis.suggested_pos[0]
        #      (subset trovato cercando combinazioni di righe OdA che sommano l'imp.)
        #   2) MATCH_PARZIALE_OK ledger pattern (RWS-style): line_ids da
        #      analysis.partial_match_subset_lines popolato da
        #      _try_oda_ledger_subset_match nel CASO 3 quando la fattura consuma
        #      una/piu' righe libere di un OdA-ledger ricorrente.
        suggested_pos = getattr(analysis, 'suggested_pos', None) or []
        ledger_subset = getattr(analysis, 'partial_match_subset_lines', None) or []

        is_subset_suggested = (
            analysis.classification in ('MATCH_DA_SUGGERIMENTO',
                                         'MATCH_DA_SUGGERIMENTO_PIU_EXTRA')
            and suggested_pos and suggested_pos[0].get('match_line_ids'))
        is_subset_ledger = (
            analysis.classification == 'MATCH_PARZIALE_OK' and ledger_subset)

        extra_amount = 0.0
        subset_ids = None
        subset_source = None

        if is_subset_suggested:
            subset_ids = set(suggested_pos[0]['match_line_ids'])
            extra_amount = float(suggested_pos[0].get('extra_amount') or 0)
            subset_source = 'MATCH_DA_SUGGERIMENTO'
        elif is_subset_ledger:
            subset_ids = set(ledger_subset)
            subset_source = 'MATCH_PARZIALE_OK (ledger)'

        if subset_ids is not None:
            po_lines_data = [pl for pl in po_lines_data if pl['id'] in subset_ids]
            if not po_lines_data:
                return WriteResult(success=False, action='create_draft_from_oda',
                                   error_message=f"Subset {subset_source} "
                                                 f"{sorted(subset_ids)} non trovato "
                                                 f"tra le righe OdA",
                                   dry_run=self.dry_run)

        # === Pattern operatore: aggiunta POL extra all'OdA ===
        # Per MATCH_PARZIALE_OK con righe extra (analysis.partial_extra_lines
        # popolato da _try_partial_match), creo una purchase.order.line
        # dedicata sull'OdA per ogni riga extra. Replica il flusso operatore
        # storico (audit step14b: 100% righe accessorie collegate a POL).
        from config.rules import ADD_EXTRA_POL_TO_ODA_ENABLED
        added_po_line_ids = []
        partial_extra_lines = getattr(analysis, 'partial_extra_lines', None) or []
        is_partial_with_extras = (
            analysis.classification == 'MATCH_PARZIALE_OK'
            and partial_extra_lines
            and ADD_EXTRA_POL_TO_ODA_ENABLED)

        if is_partial_with_extras and po_lines_data:
            # Uso la prima POL come template per UoM/analytic
            template_pol = po_lines_data[0]
            invoice_date_planned = analysis.xml_data.data or None
            for extra in partial_extra_lines:
                # Skip silenzioso delle righe extra a importo 0 (descrizioni
                # informative del fornitore senza addebito reale, es. Wuerth
                # "SPESE-GESTIONE-INCASSO €0", "SPESE-SPEDIZIONE-MINIMO €0").
                if hasattr(extra, 'prezzo_totale'):
                    extra_amount = float(extra.prezzo_totale or 0)
                else:
                    extra_amount = float(extra.get('prezzo_totale')
                                          or extra.get('prezzo') or 0)
                extra_desc = (extra.get('descrizione') if isinstance(extra, dict)
                               else getattr(extra, 'descrizione', '')) or ''
                if extra_amount <= 0:
                    logger.info(
                        f"Skip riga extra a importo 0: '{extra_desc[:60]}'")
                    continue

                new_pol = self._add_extra_pol_to_oda(
                    po_id=po.get('id'),
                    extra_line_xml=extra,
                    template_po_line=template_pol,
                    invoice_date=invoice_date_planned)
                if new_pol is None:
                    # _add_extra_pol_to_oda ritorna None per amount > soglia
                    # (amount <= 0 è già skippato sopra)
                    from config.rules import ADD_EXTRA_POL_MAX_AMOUNT
                    self._cleanup_extra_pols(added_po_line_ids)
                    return WriteResult(
                        success=False, action='create_draft_from_oda',
                        error_message=(
                            f"Riga extra oltre soglia €{ADD_EXTRA_POL_MAX_AMOUNT:.2f}: "
                            f"'{extra_desc[:60]}' (€{extra_amount:.2f})"),
                        dry_run=self.dry_run)
                if new_pol.get('_reused_existing'):
                    # POL libera gia' presente sull'OdA: NON va aggiunta in
                    # po_lines_data (lo e' gia') e NON va registrata in
                    # added_po_line_ids (sarebbe annullata dal cleanup in
                    # caso di fail successivo). Setto solo l'override conto
                    # sulla POL gia' presente.
                    for i, pl in enumerate(po_lines_data):
                        if pl.get('id') == new_pol.get('id'):
                            pl['_account_id_override'] = new_pol.get('_account_id_override')
                            pl['_extra_categoria'] = new_pol.get('_extra_categoria')
                            break
                else:
                    # Aggiungo la nuova POL al pool da fatturare
                    po_lines_data.append(new_pol)
                    if new_pol.get('id'):
                        added_po_line_ids.append(new_pol['id'])
            if added_po_line_ids:
                logger.info(
                    f"Aggiunte {len(added_po_line_ids)} POL extra all'OdA "
                    f"{po.get('name')}: ids={added_po_line_ids}")

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

            # === Override per POL extra (trasporto/bolli/...) ===
            # Le POL aggiunte da _add_extra_pol_to_oda hanno un override esplicito
            # del conto contabile (es. 420110 per trasporto). Bypassa la
            # heuristica per garantire che la riga finisca sul conto corretto.
            override_acc = pl.get('_account_id_override')
            if override_acc:
                billable_lines.append((pl, qty_to_invoice, prod_data, override_acc))
                continue

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
            self._cleanup_extra_pols(added_po_line_ids)
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
            self._cleanup_extra_pols(added_po_line_ids)
            return WriteResult(
                success=False, action='create_draft_from_oda',
                error_message=f"Merce non ancora ricevuta dal magazzino su "
                             f"{len(not_received)} righe: {details}",
                dry_run=self.dry_run,
            )

        if not billable_lines:
            self._cleanup_extra_pols(added_po_line_ids)
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=f"OdA {po.get('name')}: nessuna riga "
                                            f"da fatturare (tutto già fatturato)",
                               dry_run=self.dry_run)

        # --- Verifica totale: somma billable_lines ≈ imponibile fattura XML ---
        # Usa discount della POL (in %): subtotal = qty * price_unit * (1 - discount/100).
        # Necessario per OdA con sconto: senza questo, qty*price_unit > price_subtotal
        # reale e il check sballerebbe (es. IC Intracom 600x2.70 con discount 3% =
        # 1571.40 effettivi, non 1620 nominali).
        def _line_billable_amount(pl, qty):
            pu = float(pl.get('price_unit') or 0)
            disc = float(pl.get('discount') or 0)
            return qty * pu * (1.0 - disc / 100.0)
        sum_billable = sum(_line_billable_amount(pl, qty)
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
            self._cleanup_extra_pols(added_po_line_ids)
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
            self._cleanup_extra_pols(added_po_line_ids)
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
            self._cleanup_extra_pols(added_po_line_ids)
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
            # Propaga discount POL al move_line: il subtotale fattura applica
            # lo stesso sconto della POL (es. IC Intracom OdA al -3%).
            pol_discount = float(pl.get('discount') or 0)
            if pol_discount:
                ml_vals['discount'] = pol_discount
            if isinstance(prod, list):
                ml_vals['product_id'] = prod[0]
            uom = pl.get('product_uom')
            if isinstance(uom, list):
                ml_vals['product_uom_id'] = uom[0]
            analytic = pl.get('account_analytic_id')
            if isinstance(analytic, list) and analytic:
                ml_vals['analytic_account_id'] = analytic[0]
            move_line_vals.append((0, 0, ml_vals))

        # Per MATCH_DA_SUGGERIMENTO_PIU_EXTRA: aggiungo una riga col delta
        # tra imponibile fattura e somma righe OdA matchate.
        # Due rami:
        #  1) |delta| <= ROUNDING_THRESHOLD_AMOUNT -> ARROTONDAMENTO: la riga
        #     eredita OdA / product / UoM / commessa / IVA dalla prima POL
        #     matchata e usa il conto 410100 merci c/acquisti.
        #  2) |delta| > soglia -> SPESE ACCESSORIE legacy su 420110, no aggancio
        #     OdA (caso Sonepar trasporto +EUR 5 ecc.).
        if (analysis.classification == 'MATCH_DA_SUGGERIMENTO_PIU_EXTRA'
                and abs(extra_amount) > 0.01):
            from config.rules import (ROUNDING_THRESHOLD_AMOUNT,
                                      ROUNDING_ACCOUNT_ID_ECOTEL)
            # Convenzione segni:
            # - Fattura normale: qty=1, price=extra_amount (mantiene segno).
            # - Nota di credito: qty=-1, price=extra_amount -> subtotal segno
            #   speculare al non-NC, coerente con la convenzione Ecotel
            #   (qty=-1, price=-X -> subtotal positivo).
            extra_qty = -1 if is_nota_credito else 1
            extra_price = extra_amount

            first_pl = billable_lines[0][0] if billable_lines else None
            pl_taxes = (first_pl.get('taxes_id') or []) if first_pl else []

            if (abs(extra_amount) <= ROUNDING_THRESHOLD_AMOUNT
                    and first_pl is not None):
                extra_ml = {
                    'name': 'Arrotondamento',
                    'quantity': extra_qty,
                    'price_unit': extra_price,
                    'account_id': ROUNDING_ACCOUNT_ID_ECOTEL,
                    'purchase_line_id': first_pl['id'],
                    'tax_ids': [(6, 0, list(pl_taxes))] if pl_taxes
                               else [(6, 0, [])],
                }
                prod = first_pl.get('product_id')
                if isinstance(prod, list) and prod:
                    extra_ml['product_id'] = prod[0]
                uom = first_pl.get('product_uom')
                if isinstance(uom, list) and uom:
                    extra_ml['product_uom_id'] = uom[0]
                analytic = first_pl.get('account_analytic_id')
                if isinstance(analytic, list) and analytic:
                    extra_ml['analytic_account_id'] = analytic[0]
                logger.info(
                    f"Aggiunta riga arrotondamento: EUR {extra_price:+.2f} "
                    f"agganciata a POL {first_pl['id']} (OdA {po.get('name')})"
                )
            else:
                # Spese accessorie reali (delta grande, es. trasporto)
                extra_ml = {
                    'name': 'Spese accessorie (trasporto/contributi)',
                    'quantity': extra_qty,
                    'price_unit': extra_price,
                    'account_id': 125,  # 420110 trasporto e spedizione
                }
                if pl_taxes:
                    extra_ml['tax_ids'] = [(6, 0, list(pl_taxes))]
                logger.info(
                    f"Aggiunta riga spese accessorie: EUR {extra_price:+.2f}"
                )

            move_line_vals.append((0, 0, extra_ml))

        # Payment term dal partner
        payment_term_id = self._get_partner_payment_term(partner_id)

        # Data competenza IVA = fine mese
        data_contabile = self._data_contabile(analysis, invoice_date)
        data_competenza_iva = self._end_of_month(invoice_date)

        # Guard: l'agent opera SOLO su Ecotel (company_id=1).
        # Se il PO appartiene a un'altra company, blocco per evitare scritture
        # accidentali multi-company (e per evitare conflitti su partner condivisi).
        if not company_id:
            company_id = 1  # default Ecotel se mancante
        if company_id != 1:
            self._cleanup_extra_pols(added_po_line_ids)
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
            'date': data_contabile,
            'l10n_it_vat_settlement_date': data_competenza_iva,
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
                added_po_line_ids=added_po_line_ids or None,
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
                added_po_line_ids=added_po_line_ids or None,
            )
        except Exception as e:
            logger.exception("Errore create_bozza_da_oda_matched")
            # Compensazione: se ho aggiunto POL extra ma il move è fallito,
            # uso l'helper centralizzato che gestisce anche OdA in state=purchase
            # (fallback [ANNULLATA]).
            self._cleanup_extra_pols(added_po_line_ids)
            return WriteResult(success=False, action='create_draft_from_oda',
                               error_message=str(e), dry_run=False)

    def create_bozza_autostrade(self, analysis, mapping_entry: Dict,
                                  pdf_split: Optional[Dict] = None) -> WriteResult:
        """
        Crea bozza per fattura Autostrade per l'Italia (IT07516911000).

        Pattern decifrato in plans/crispy-napping-tower.md sez. 3.2:

        - cc Ecotel main (261713569 / 217718183 / 216875601 / 311531633) →
          OdA P03718, consume-POL: cerca 2 POL libere (1 furgoni + 1 uso
          promiscuo) matchando cc + classificazione nella descrizione,
          aggiorna price_unit/name/qty_received e collega le move_line via
          purchase_line_id (pattern Trenitalia/Italo dal 07/05/2026).
          - Se pdf_split fornito (R4): usa i 2 importi calcolati dal parser
            PDF + APPARATI_MAP.
          - Altrimenti (R1 fallback): 2 righe a importo 0 — il contabile
            aggiorna manualmente in Odoo. La bozza ha comunque conti, IVA,
            narrazione, cc, OdA pre-popolati.
          - Selezione POL: priorità a POL cc-specifica (descrizione contiene
            solo quel cc), poi POL jolly (descrizione multi-cc) come
            fallback.

        cc ex-Utterson e residuo chiusi 07/05/2026: il classifier non li
        risolve più, eventuali fatture residue cadono in DA_VERIFICARE.
        Le branche cc_type='ex_utterson'/'residuo' qui sotto restano per
        compatibilità storica ma non sono più raggiunte dal flow normale.

        WriteResult: po_line_id+old_* della 1ª POL (furgoni), extra_po_lines
        per la 2ª (uso_promiscuo). Audit/rollback usano entrambe.

        Args:
            analysis: FatturaPAAnalysis con xml_data e codice_cliente popolati
            mapping_entry: voce di MAPPATURA_FORNITORI_FISSI già risolta
                           (con `cc_type` e `oda_fisso` del contratto matchato)
            pdf_split: dict opzionale dell'output di
                       `core.pdf_parser.calcola_split_furgoni_promiscuo`

        Returns: WriteResult con success/move_id/error_message.
        """
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01',):
            # Autostrade emette solo TD01 (verificato 80 XML in v3 sez. 1.2)
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message=f"Tipo {tipo_doc} non supportato "
                                             f"per Autostrade (atteso TD01)",
                               dry_run=self.dry_run)

        cc = getattr(analysis.xml_data, 'codice_cliente', None)
        if not cc:
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message="codice_cliente mancante in XML",
                               dry_run=self.dry_run)

        cc_type = mapping_entry.get('cc_type')
        oda_name = mapping_entry.get('oda_fisso')
        if not cc_type or not oda_name:
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message=f"mapping_entry incompleto: "
                                             f"cc_type={cc_type}, oda={oda_name}",
                               dry_run=self.dry_run)

        # Cerca OdA su Odoo (Ecotel only)
        ECOTEL = 1
        pos = self.client._call('purchase.order', 'search_read',
            [('name', '=', oda_name), ('company_id', '=', ECOTEL)],
            fields=['id', 'name', 'state', 'partner_id', 'currency_id'],
            limit=1)
        if not pos:
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message=f"OdA {oda_name} non trovato su Ecotel",
                               dry_run=self.dry_run)
        po = pos[0]
        po_id = po['id']
        po_name = po['name']
        partner_id = (po['partner_id'][0]
                      if isinstance(po['partner_id'], list) else None)
        currency_id = (po['currency_id'][0]
                       if isinstance(po['currency_id'], list) else None)

        if not partner_id:
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message=f"OdA {oda_name} senza partner",
                               dry_run=self.dry_run)

        # Imponibile XML totale fattura
        imponibile_xml = float(analysis.xml_data.imponibile_totale or 0)
        if imponibile_xml <= 0:
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message="imponibile XML <= 0",
                               dry_run=self.dry_run)

        # Determina le righe move da creare in base a cc_type + pdf_split
        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        date_contabile = self._data_contabile(analysis, invoice_date)
        date_iva = self._end_of_month(invoice_date)
        tax_id = (mapping_entry.get('taxes_id') or [11])[0]  # 22% S default
        conto_furgoni = mapping_entry.get('conto_furgoni_id', 368)
        conto_promiscuo = mapping_entry.get('conto_promiscuo_id', 1124)

        move_lines_vals = []
        # POL info per consume-POL pattern (Autostrade ecotel_main).
        # Lista di dict {po_line_id, old_price_unit, old_name, old_date_planned,
        # cls_token, new_price, new_name, account_id, product_id, product_uom_id,
        # account_analytic_id} — usata per audit/rollback E per popolare le
        # move_line_vals con purchase_line_id corretto.
        consumed_pol_info: List[Dict] = []

        if cc_type == 'ecotel_main':
            # Consume-POL: per la fattura cerchiamo 2 POL libere su P03718,
            # una furgoni e una promiscuo, matchate per cc + classificazione
            # nella descrizione. Pattern Trenitalia/Italo applicato.
            if pdf_split:
                imp_furg = float(pdf_split.get('imponibile_furgoni', 0))
                imp_prom = float(pdf_split.get('imponibile_promiscuo', 0))
            else:
                imp_furg = 0.0
                imp_prom = 0.0

            chosen_ids: set = set()
            for cls, conto, imp_val in (
                ('furgoni', conto_furgoni, imp_furg),
                ('uso_promiscuo', conto_promiscuo, imp_prom),
            ):
                cands = self._find_pol_autostrade_match(po_id, cc, cls,
                                                          excluded_ids=chosen_ids)
                if not cands:
                    return WriteResult(
                        success=False, action='create_draft_autostrade',
                        error_message=(
                            f"Nessuna POL libera trovata su {po_name} per "
                            f"cc={cc} classificazione={cls}. "
                            f"Aggiungere righe disponibili sull'OdA."),
                        dry_run=self.dry_run)
                pol = cands[0]
                chosen_ids.add(pol['id'])
                # Estraggo product_id, uom, analytic dalla POL
                _prod = pol.get('product_id')
                product_id = _prod[0] if isinstance(_prod, list) else _prod
                _uom = pol.get('product_uom')
                product_uom_id = _uom[0] if isinstance(_uom, list) else _uom
                _aa = pol.get('account_analytic_id')
                analytic_id = _aa[0] if isinstance(_aa, list) else _aa
                cls_label = 'furgoni' if cls == 'furgoni' else 'uso promiscuo'
                # Formato allineato alle POL inserite manualmente dagli
                # operatori sullo stesso OdA P03718 (pattern verificato su
                # 20+ POL gen-mar 2026: stringa multi-line con 3 righe
                # separate da \n). Niente prefisso OdA, niente FT/data.
                new_name = (f"PEDAGGI AUTOSTRADALI\n"
                             f"Codice cliente: {cc}\n"
                             f"{cls_label}")
                consumed_pol_info.append({
                    'po_line_id': pol['id'],
                    'old_price_unit': pol.get('price_unit') or 0,
                    'old_name': pol.get('name') or '',
                    'old_date_planned': pol.get('date_planned') or None,
                    'cls': cls,
                    'cls_label': cls_label,
                    'new_price': round(imp_val, 2),
                    'new_name': new_name,
                    'account_id': conto,
                    'product_id': product_id,
                    'product_uom_id': product_uom_id,
                    'analytic_account_id': analytic_id,
                })
                ml_vals = {
                    'name': new_name,
                    'account_id': conto,
                    'price_unit': round(imp_val, 2),
                    'quantity': 1,
                    'tax_ids': [(6, 0, [tax_id])],
                    'purchase_line_id': pol['id'],
                }
                if product_id:
                    ml_vals['product_id'] = product_id
                if product_uom_id:
                    ml_vals['product_uom_id'] = product_uom_id
                if analytic_id:
                    ml_vals['analytic_account_id'] = analytic_id
                move_lines_vals.append(ml_vals)
        elif cc_type in ('ex_utterson', 'residuo'):
            # 1 sola riga totale in 420160
            move_lines_vals.append({
                'name': f"{po_name}: Pedaggi Autostradali "
                        f"({'ex UTT ' if cc_type == 'ex_utterson' else ''})"
                        f"FT n. {invoice_number} del {invoice_date} | "
                        f"Codice cliente: {cc}",
                'account_id': conto_furgoni,  # 420160 sempre
                'price_unit': round(imponibile_xml, 2),
                'quantity': 1,
                'tax_ids': [(6, 0, [tax_id])],
            })
        else:
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message=f"cc_type sconosciuto: {cc_type}",
                               dry_run=self.dry_run)

        # Journal acquisti Ecotel
        journal_id = mapping_entry.get('journal_id', 2)

        # Costruzione vals move
        move_vals = {
            'move_type': 'in_invoice',
            'partner_id': partner_id,
            'invoice_date': invoice_date,
            'date': date_contabile,
            'l10n_it_vat_settlement_date': date_iva,
            'ref': invoice_number,
            'invoice_origin': po_name,
            'journal_id': journal_id,
            'company_id': ECOTEL,
            'currency_id': currency_id,
            'invoice_line_ids': [(0, 0, ml) for ml in move_lines_vals],
        }

        if self.dry_run:
            tot_lines = sum(ml['price_unit'] for ml in move_lines_vals)
            consumed_str = ', '.join(
                f"POL {p['po_line_id']} ({p['cls']}, "
                f"€{p['old_price_unit']:.2f}->€{p['new_price']:.2f})"
                for p in consumed_pol_info)
            logger.info(
                f"[DRY_RUN] create_bozza_autostrade cc={cc} cc_type={cc_type} "
                f"OdA={po_name} righe={len(move_lines_vals)} "
                f"consume-POL=[{consumed_str}] "
                f"tot_imponibile_lines={tot_lines:.2f} "
                f"vs imponibile_xml={imponibile_xml:.2f} "
                f"pdf_split={'YES' if pdf_split else 'no (R1 manual fallback)'}"
            )
            # Per dry-run popolo po_line_id+old_* della prima POL e
            # extra_po_lines col resto (compatibile con audit DB)
            primary = consumed_pol_info[0] if consumed_pol_info else {}
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'cls': p['cls']}
                for p in consumed_pol_info[1:]
            ] if consumed_pol_info else None
            return WriteResult(success=True, action='create_draft_autostrade',
                               move_id=None, dry_run=True,
                               po_line_id=primary.get('po_line_id'),
                               old_price_unit=primary.get('old_price_unit'),
                               old_name=primary.get('old_name'),
                               old_date_planned=primary.get('old_date_planned'),
                               extra_po_lines=extras)

        try:
            # Step 1: aggiorno le 2 POL libere col prezzo+nome della fattura
            # PRIMA di creare il move (così il purchase_line_id è valido).
            for p in consumed_pol_info:
                self.client._call('purchase.order.line', 'write',
                    [p['po_line_id']], {
                        'price_unit': p['new_price'],
                        'name': p['new_name'],
                        'product_qty': 1,
                        'qty_received': 1,
                        'qty_received_manual': 1,
                        'date_planned': invoice_date,
                    })
                logger.info(f"Updated POL {p['po_line_id']} ({p['cls']}): "
                           f"price={p['new_price']:.2f}, qty_received=1")

            # Step 2: creo account.move con le 2 move_line linkate alle POL
            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            if not move_id:
                raise RuntimeError("create move returned empty")
            logger.info(f"Created Autostrade move {move_id} cc={cc} OdA={po_name} "
                       f"consume-POL ids={[p['po_line_id'] for p in consumed_pol_info]}")

            # Allego XML al move
            if analysis.raw_xml:
                try:
                    attachment_vals = {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    }
                    self.client._call('ir.attachment', 'create', attachment_vals)
                except Exception as e:
                    logger.warning(f"Allegato XML fallito (non blocca): {e}")

            # Marco fatturapa.attachment.in come registered
            if analysis.attachment_id:
                try:
                    self.client._call('account.move', 'write', [move_id], {
                        'fatturapa_attachment_in_id': analysis.attachment_id,
                    })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito (non blocca): {e}")

            # Audit per rollback
            primary = consumed_pol_info[0] if consumed_pol_info else {}
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'cls': p['cls']}
                for p in consumed_pol_info[1:]
            ] if consumed_pol_info else None
            return WriteResult(success=True, action='create_draft_autostrade',
                               move_id=move_id, dry_run=False,
                               po_line_id=primary.get('po_line_id'),
                               old_price_unit=primary.get('old_price_unit'),
                               old_name=primary.get('old_name'),
                               old_date_planned=primary.get('old_date_planned'),
                               extra_po_lines=extras)
        except Exception as e:
            logger.exception("Errore create_bozza_autostrade")
            # Best-effort restore delle POL già aggiornate prima dell'errore
            for p in consumed_pol_info:
                try:
                    self.client._call('purchase.order.line', 'write',
                        [p['po_line_id']], {
                            'price_unit': p['old_price_unit'],
                            'name': p['old_name'],
                            'qty_received': 0,
                            'qty_received_manual': 0,
                        })
                except Exception:
                    logger.warning(f"Restore POL {p['po_line_id']} fallito")
            return WriteResult(success=False, action='create_draft_autostrade',
                               error_message=str(e), dry_run=False)

    # === Automezzi (7 fornitori noleggio veicoli) === #

    # Mappa aliquota XML -> tax_id Odoo per fallback (quando il fornitore
    # non ha override per voce/classificazione).
    _ALIQUOTA_TO_TAX_DEFAULT = {
        22.0: 11,    # 22% S
        10.0: 21,    # 10% S (se mai usato)
        4.0: 18,     # 4% S
    }

    @staticmethod
    def _classify_voce_automezzi(desc: str) -> str:
        """Identifica voce di una riga XML automezzi.

        Ritorna: 'locazione' | 'servizi' | 'tassa' | 'spese_incasso'.
        Default 'locazione' (caso più frequente).
        """
        d = (desc or '').upper()
        # Spese di incasso bancarie (Tecnoalt €3.50): conto 420410 oneri bancari
        if any(tok in d for tok in (
                'SPESE DI INCASSO', 'SPESE INCASSO', 'SPESE D\'INCASSO')):
            return 'spese_incasso'
        # Tassa/bollo prima (più specifico)
        if any(tok in d for tok in (
                'BOLLO', 'TASSA DI PROPRIETA', 'TASSA AUTOMOBILISTICA',
                'RIADDEBITO TASSE', 'SUPERBOLLO', 'TASSA DI POSSESSO')):
            return 'tassa'
        # Servizi (specifico)
        if any(tok in d for tok in (
                'GESTIONE E SERVIZI', 'CANONE SERVIZIO', 'CANONE SERVIZI',
                'CANONESERVIZIO')):
            return 'servizi'
        # Default = locazione
        return 'locazione'

    @staticmethod
    def _pol_product_matches_voce(pol: Dict, voce: str) -> bool:
        """Decide se una POL libera è adatta per una riga XML di una certa voce.

        Match logic:
          voce='spese_incasso' -> POL con product_id che contiene 'incasso' o
            'spese' (es. '[Spese Incasso] Spese Incasso')
          altre voci -> POL con product_id che NON contiene 'incasso/spese'
            (es. 'noleggio', 'Fornitura di Servizi', 'PARCHEGGIO')
        """
        prod = pol.get('product_id')
        if not isinstance(prod, list) or len(prod) < 2:
            return True  # POL senza product_id, accetta come jolly
        prod_name = (prod[1] or '').lower()
        is_incasso_pol = any(k in prod_name for k in ('incasso', 'spese'))
        if voce == 'spese_incasso':
            return is_incasso_pol
        return not is_incasso_pol

    @staticmethod
    def _extract_targa_automezzi(riga, raw_xml_line: str = '') -> str:
        """Estrae targa da una riga XML.

        Strategia (in ordine di priorita'):
          1. CodiceArticolo con CodiceTipo='TARGA' (Tecnoalt FatturaPA std)
          2. AltriDatiGestionali con TipoDato='Targa' (Athlon/UnipolRental)
          3. Regex sulla descrizione testuale (Leasys/ALD/Arval)
          4. Regex su raw_xml_line se passato (fallback)
        """
        if not riga:
            return ''
        # 1. CodiceArticolo TARGA strutturato (Tecnoalt)
        codici = getattr(riga, 'codici_articolo', None) or {}
        targa_struct = (codici.get('TARGA') or codici.get('Targa') or
                         codici.get('targa') or '')
        if targa_struct:
            m0 = re.search(r'\b([A-Z]{2}\d{3}[A-Z]{2})\b', str(targa_struct).upper())
            if m0:
                return m0.group(1)
        # 2. AltriDatiGestionali TARGA (Athlon, UnipolRental)
        adg = getattr(riga, 'altri_dati_gestionali', None) or {}
        targa_adg = adg.get('TARGA') or adg.get('Targa') or ''
        if targa_adg:
            m1 = re.search(r'\b([A-Z]{2}\d{3}[A-Z]{2})\b', str(targa_adg).upper())
            if m1:
                return m1.group(1)
        # 3. Regex sulla descrizione
        desc = riga.descrizione or ''
        m = re.search(r'\b([A-Z]{2}\d{3}[A-Z]{2})\b', desc.upper())
        if m:
            return m.group(1)
        # 4. Fallback su raw_xml_line passato
        if raw_xml_line:
            m2 = re.search(r'\b([A-Z]{2}\d{3}[A-Z]{2})\b', raw_xml_line.upper())
            if m2:
                return m2.group(1)
        return ''

    @staticmethod
    def _extract_numero_contratto_unipol(desc: str) -> str:
        """Per UnipolRental: estrae numero contratto da regex 'Contr. n.NNN'."""
        if not desc:
            return ''
        m = re.search(r'Contr\.?\s*n\.?\s*([\w\d\-]+)', desc, re.IGNORECASE)
        return m.group(1).strip() if m else ''

    def _resolve_oda_automezzi(self, mapping_entry: Dict, riga,
                                  numero_contratto_xml: str) -> str:
        """Risolve OdA target per una riga XML.

        Priorità:
        1. Se multi_contratto: cerca numero_contratto_xml in
           mapping_entry['contratti'] -> oda_fisso
        2. Fallback su mapping_entry['oda_default'] (multi_contratto)
        3. Per fornitori non multi_contratto: mapping_entry['oda_fisso']
        """
        if mapping_entry.get('multi_contratto'):
            contratti = mapping_entry.get('contratti', {})
            sub = contratti.get(numero_contratto_xml)
            if sub:
                return sub.get('oda_fisso')
            return mapping_entry.get('oda_default')
        return mapping_entry.get('oda_fisso')

    def _auto_discover_oda_by_targa(self, partner_id: int, targa: str,
                                      company_id: int = 1,
                                      excluded_oda_names: Optional[set] = None) -> Optional[str]:
        """Auto-discovery OdA aperto del fornitore con POL libere che cita la targa.

        Pattern Tecnoalt-style: 1 OdA per veicolo. Se il numero contratto
        della fattura non è nella mappatura statica (config/rules.py), cerca
        tra gli OdA aperti del fornitore quale ha POL libere col `name` che
        contiene la targa.

        Restituisce il `name` dell'OdA (es. 'P02976') o None se non univoco.
        """
        if not partner_id or not targa:
            return None
        excluded = excluded_oda_names or set()
        # Tutti gli OdA aperti (state=purchase) del fornitore
        pos = self.client._call('purchase.order', 'search_read',
            [('partner_id', '=', partner_id),
             ('company_id', '=', company_id),
             ('state', '=', 'purchase')],
            fields=['id', 'name'], limit=50)
        candidates = []
        for po in pos:
            if po['name'] in excluded:
                continue
            # Cerca POL libere su questo OdA che citano la targa
            pol_count = self.client._call('purchase.order.line', 'search_count',
                [('order_id', '=', po['id']),
                 ('name', 'ilike', targa),
                 ('qty_invoiced', '=', 0),
                 ('qty_received', '=', 0)])
            if pol_count > 0:
                candidates.append((po['name'], pol_count))
        if len(candidates) == 1:
            return candidates[0][0]
        if len(candidates) > 1:
            # Più OdA candidati: prendi quello con più POL libere matching
            candidates.sort(key=lambda x: -x[1])
            logger.warning(f"Auto-discovery: {len(candidates)} OdA candidati "
                          f"per targa={targa}, scelto {candidates[0][0]} "
                          f"(con {candidates[0][1]} POL libere matching)")
            return candidates[0][0]
        return None

    def _resolve_classificazione_veicolo(self, targa: str,
                                           numero_contratto_xml: str,
                                           mapping_entry: Optional[Dict] = None) -> tuple:
        """Risolve classificazione fiscale veicolo da PARCO_BY_TARGA / PARCO_BY_CONTRATTO.

        Strategia (in ordine):
          1. Lookup PARCO_BY_TARGA[targa]
          2. Lookup PARCO_BY_CONTRATTO[numero]
          3. Override per fornitore: mapping_entry.classificazione_default
             (es. Tecnoalt = sempre POOL anche se targa non in parco)
          4. Default conservativo: uso_promiscuo

        Ritorna (classificazione, source) dove source =
        'targa' | 'contratto' | 'fornitore_default' | 'default_unknown'.
        """
        try:
            from config.parco_auto_mapping import (
                get_classificazione_by_targa, get_classificazione_by_contratto)
        except ImportError:
            # No parco mapping: applica fornitore_default se c'è
            if mapping_entry and mapping_entry.get('classificazione_default'):
                return (mapping_entry['classificazione_default'], 'fornitore_default')
            return ('uso_promiscuo', 'default_no_mapping')
        if targa:
            cls = get_classificazione_by_targa(targa)
            if cls:
                return (cls, 'targa')
        if numero_contratto_xml:
            cls = get_classificazione_by_contratto(numero_contratto_xml)
            if cls:
                return (cls, 'contratto')
        # 3. Override fornitore (es. Tecnoalt = POOL sempre)
        if mapping_entry and mapping_entry.get('classificazione_default'):
            return (mapping_entry['classificazione_default'], 'fornitore_default')
        return ('uso_promiscuo', 'default_unknown')

    def create_bozza_automezzi(self, analysis,
                                 mapping_entry: Dict) -> WriteResult:
        """Crea bozza per fattura noleggio veicoli (7 fornitori automezzi).

        Pattern consume-POL multi-line con riscrittura totale di POL libere
        generic "TEST €1" pre-create da Acquisti su OdA-ledger annuali (o
        OdA-per-veicolo per Tecnoalt).

        Per ogni riga XML:
          1. Identifica voce (locazione/servizi/tassa) via descrizione
          2. Estrai targa + numero contratto dalla riga
          3. Lookup PARCO_BY_TARGA/CONTRATTO -> classificazione
             (POOL/uso_promiscuo/super_lusso). Default uso_promiscuo.
          4. Conto = CONTO_AUTOMEZZI[(voce, classificazione)]
          5. Tax_id = TAX_AUTOMEZZI[vat][(voce, classificazione)] con
             fallback (voce, '*'). Se non trovato, prendi da aliquota_iva XML.
          6. Risolvi OdA dalla mapping_entry (multi_contratto o singolo)
          7. Prendi prima POL libera dell'OdA (FIFO id), riscrivi
             completamente: name, price_unit, taxes_id, qty_received=1
          8. Crea move_line con purchase_line_id collegato

        Audit con extra_po_lines per N>1 POL consumate (schema esistente
        extra_po_lines_json).

        Returns WriteResult con po_line_id (1ª POL) + extra_po_lines (2ª-Nª).
        """
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01', 'TD04'):
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message=f"Tipo doc {tipo_doc} non supportato "
                                             f"per Automezzi (atteso TD01/TD04)",
                               dry_run=self.dry_run)

        is_nota_credito = (tipo_doc == 'TD04')

        # Risolvo P.IVA fornitore + verifico in mappatura
        try:
            from config.rules import (MAPPATURA_AUTOMEZZI, CONTO_AUTOMEZZI,
                                        TAX_AUTOMEZZI)
        except ImportError:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message="config.rules MAPPATURA_AUTOMEZZI mancante",
                               dry_run=self.dry_run)

        vat = (analysis.xml_data.cedente_partita_iva or '').strip().upper()
        if vat not in MAPPATURA_AUTOMEZZI:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message=f"P.IVA {vat} non in MAPPATURA_AUTOMEZZI",
                               dry_run=self.dry_run)

        nome_forn = mapping_entry.get('nome', vat)

        # Righe XML
        righe = analysis.xml_data.righe or []
        if not righe:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message="Nessuna riga in XML",
                               dry_run=self.dry_run)

        # Date
        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        date_contabile = self._data_contabile(analysis, invoice_date)
        date_iva = self._end_of_month(invoice_date)

        # Per ogni riga: classifica voce + risolvi OdA + cls + conto + tax
        # Raggruppa per OdA (alcune fatture toccano più OdA, es. UnipolRental
        # multi-origin).
        from config.rules import resolve_mapping_entry
        # Pre-resolve: per multi_contratto serve un mapping_entry "specifico"
        # per ogni riga. Per fornitori a OdA singolo basta il parent.
        is_multi = mapping_entry.get('multi_contratto', False)

        # Per ognuna delle righe, decidiamo info di routing
        riga_info_list = []
        for riga in righe:
            desc = riga.descrizione or ''
            voce = self._classify_voce_automezzi(desc)
            targa = self._extract_targa_automezzi(riga)
            # Numero contratto: per UnipolRental da regex "Contr. n.NNN"
            # nella desc; per altri si tenta dal contratto_riferimenti XML
            # (DatiContratto.IdDocumento). Resolve fa già il match
            # contesto a livello fattura — per riga uso la desc.
            num_contratto = ''
            if vat == 'IT03740811207':  # UnipolRental
                num_contratto = self._extract_numero_contratto_unipol(desc)
            else:
                # Per Tecnoalt/ALD/Leasys cerchiamo nei contratto_riferimenti
                # globali; di solito multi-contratto significa che la fattura
                # ha N DatiContratto per N righe — il match per riga
                # richiederebbe RiferimentoNumeroLinea, qui uso il primo
                # disponibile come fallback.
                refs = getattr(analysis.xml_data, 'contratto_riferimenti', None) or []
                if refs:
                    num_contratto = refs[0].strip()

            # Risolvi OdA per questa riga
            if is_multi:
                contratti = mapping_entry.get('contratti', {})
                sub = contratti.get(num_contratto)
                if sub:
                    oda_name = sub.get('oda_fisso')
                else:
                    # Auto-discovery: cerca OdA aperto del fornitore con POL
                    # libere che citano la targa. Pattern Tecnoalt-style
                    # (1 OdA per veicolo). Funziona anche se il contratto non
                    # e' in mappatura statica.
                    oda_name = None
                    if targa:
                        oda_name = self._auto_discover_oda_by_targa(
                            mapping_entry.get('partner_id'),
                            targa,
                            company_id=mapping_entry.get('company_id', 1))
                        if oda_name:
                            logger.info(f"Auto-discovery OdA: contratto "
                                       f"{num_contratto} targa={targa} -> "
                                       f"{oda_name} (non in mappatura statica)")
                    if not oda_name:
                        oda_name = mapping_entry.get('oda_default')
            else:
                oda_name = mapping_entry.get('oda_fisso')

            if not oda_name:
                return WriteResult(success=False, action='create_draft_automezzi',
                                   error_message=(f"OdA non risolto per riga "
                                                  f"L{riga.numero_linea} fornitore {nome_forn}"),
                                   dry_run=self.dry_run)

            # Classificazione veicolo (passa mapping_entry per override fornitore_default)
            cls, cls_source = self._resolve_classificazione_veicolo(
                targa, num_contratto, mapping_entry=mapping_entry)

            # Conto
            conto = CONTO_AUTOMEZZI.get((voce, cls))
            if conto is None:
                # Fallback: voce + uso_promiscuo
                conto = CONTO_AUTOMEZZI.get((voce, 'uso_promiscuo'))
            if conto is None:
                return WriteResult(success=False, action='create_draft_automezzi',
                                   error_message=(f"Conto non risolto per "
                                                  f"voce={voce} cls={cls}"),
                                   dry_run=self.dry_run)

            # Tax_id: prima da TAX_AUTOMEZZI[vat][(voce, cls)] poi (voce, '*')
            tax_map = TAX_AUTOMEZZI.get(vat, {})
            tax_id = tax_map.get((voce, cls)) or tax_map.get((voce, '*'))
            if tax_id is None:
                # Fallback su aliquota XML
                tax_id = self._ALIQUOTA_TO_TAX_DEFAULT.get(
                    float(riga.aliquota_iva or 0), 11)

            riga_info_list.append({
                'riga': riga,
                'voce': voce,
                'targa': targa,
                'num_contratto': num_contratto,
                'cls': cls,
                'cls_source': cls_source,
                'oda_name': oda_name,
                'conto': conto,
                'tax_id': tax_id,
            })

        # Raggruppo per OdA
        oda_to_righe = {}
        for ri in riga_info_list:
            oda_to_righe.setdefault(ri['oda_name'], []).append(ri)

        # Recupero info OdA + POL libere per ognuno (1 query per OdA)
        ECOTEL = 1
        oda_state = {}   # oda_name -> {po_id, partner_id, currency_id, libere}
        for oda_name in oda_to_righe.keys():
            pos = self.client._call('purchase.order', 'search_read',
                [('name', '=', oda_name), ('company_id', '=', ECOTEL)],
                fields=['id', 'name', 'state', 'partner_id', 'currency_id'],
                limit=1)
            if not pos:
                return WriteResult(success=False, action='create_draft_automezzi',
                                   error_message=f"OdA {oda_name} non trovato su Ecotel",
                                   dry_run=self.dry_run)
            po = pos[0]
            partner_id = (po['partner_id'][0]
                           if isinstance(po['partner_id'], list) else None)
            currency_id = (po['currency_id'][0]
                            if isinstance(po['currency_id'], list) else None)
            if not partner_id:
                return WriteResult(success=False, action='create_draft_automezzi',
                                   error_message=f"OdA {oda_name} senza partner",
                                   dry_run=self.dry_run)
            libere = self._find_libere_purchase_order_lines(po['id'], 'standard_qty_inv_rec')
            n_needed = len(oda_to_righe[oda_name])
            if len(libere) < n_needed:
                return WriteResult(success=False, action='create_draft_automezzi',
                                   error_message=(f"POL libere insufficienti su {oda_name}: "
                                                  f"{len(libere)} disponibili, {n_needed} servono. "
                                                  f"Acquisti deve aggiungere altre POL TEST a {oda_name}."),
                                   dry_run=self.dry_run)
            oda_state[oda_name] = {
                'po_id': po['id'],
                'partner_id': partner_id,
                'currency_id': currency_id,
                'libere': libere,
            }

        # Assegna 1 POL libera per ogni riga: MATCH PER VOCE
        # (es. riga "Noleggio:" -> POL con product 'noleggio',
        # riga "SPESE DI INCASSO" -> POL con product '[Spese Incasso]').
        consumed_pol_info = []
        move_lines_vals = []
        # Tracking per OdA: set di POL già consumate in questa fattura
        oda_pol_used = {oda: set() for oda in oda_to_righe.keys()}

        for ri in riga_info_list:
            oda_name = ri['oda_name']
            st = oda_state[oda_name]
            voce = ri['voce']
            # Cerca prima POL libera col product matching la voce
            pol = None
            for cand in st['libere']:
                if cand['id'] in oda_pol_used[oda_name]:
                    continue
                if self._pol_product_matches_voce(cand, voce):
                    pol = cand
                    break
            # Fallback: se nessuna POL match per voce, prendi la prima libera
            # disponibile (caso edge: OdA con product_id uniformi)
            if pol is None:
                for cand in st['libere']:
                    if cand['id'] not in oda_pol_used[oda_name]:
                        pol = cand
                        break
            if pol is None:
                return WriteResult(success=False, action='create_draft_automezzi',
                                   error_message=(f"POL libere insufficienti su {oda_name} "
                                                  f"per voce={voce} (fattura ha "
                                                  f"{len(oda_to_righe[oda_name])} righe)"),
                                   dry_run=self.dry_run)
            oda_pol_used[oda_name].add(pol['id'])

            riga = ri['riga']
            # Importo riga
            price = float(riga.prezzo_totale or riga.prezzo_unitario or 0)
            # NC: prezzo negativo
            if is_nota_credito:
                price = -abs(price)

            # name allineato al pattern fornitore (preservo desc XML originale)
            desc_orig = (riga.descrizione or '').strip()
            new_name = desc_orig if desc_orig else f"{ri['voce']} automezzi"
            # Aggiungo periodo se presente
            data_inizio = getattr(riga, 'data_inizio_periodo', None) if hasattr(riga, 'data_inizio_periodo') else None
            data_fine = getattr(riga, 'data_fine_periodo', None) if hasattr(riga, 'data_fine_periodo') else None

            # Estraggo product/uom dalla POL
            _prod = pol.get('product_id')
            product_id = _prod[0] if isinstance(_prod, list) else _prod
            _uom = pol.get('product_uom')
            product_uom_id = _uom[0] if isinstance(_uom, list) else _uom
            _aa = pol.get('account_analytic_id')
            analytic_id = _aa[0] if isinstance(_aa, list) else _aa

            consumed_pol_info.append({
                'po_line_id': pol['id'],
                'oda_name': oda_name,
                'old_price_unit': pol.get('price_unit') or 0,
                'old_name': pol.get('name') or '',
                'old_date_planned': pol.get('date_planned') or None,
                'old_taxes_id': list(pol.get('taxes_id') or []),
                'voce': ri['voce'],
                'cls': ri['cls'],
                'cls_source': ri['cls_source'],
                'targa': ri['targa'],
                'num_contratto': ri['num_contratto'],
                'new_price': round(price, 2),
                'new_name': new_name,
                'new_tax_id': ri['tax_id'],
                'account_id': ri['conto'],
                'product_id': product_id,
                'product_uom_id': product_uom_id,
                'analytic_account_id': analytic_id,
            })
            qty_move = -1 if is_nota_credito else 1
            ml_vals = {
                'name': new_name,
                'account_id': ri['conto'],
                'price_unit': round(price, 2),
                'quantity': qty_move,
                'tax_ids': [(6, 0, [ri['tax_id']])],
                'purchase_line_id': pol['id'],
            }
            if product_id:
                ml_vals['product_id'] = product_id
            if product_uom_id:
                ml_vals['product_uom_id'] = product_uom_id
            if analytic_id:
                ml_vals['analytic_account_id'] = analytic_id
            move_lines_vals.append(ml_vals)

        # Move vals (uso il primo OdA come invoice_origin "principale")
        primary_oda = list(oda_to_righe.keys())[0]
        primary_partner = oda_state[primary_oda]['partner_id']
        primary_currency = oda_state[primary_oda]['currency_id']
        # Se più OdA: invoice_origin elenca tutti separati da virgola
        all_odas = ','.join(oda_to_righe.keys())

        move_type = 'in_refund' if is_nota_credito else 'in_invoice'
        journal_id = mapping_entry.get('journal_id', 2)
        move_vals = {
            'move_type': move_type,
            'partner_id': primary_partner,
            'invoice_date': invoice_date,
            'date': date_contabile,
            'l10n_it_vat_settlement_date': date_iva,
            'ref': invoice_number,
            'invoice_origin': all_odas,
            'journal_id': journal_id,
            'company_id': ECOTEL,
            'currency_id': primary_currency,
            'invoice_line_ids': [(0, 0, ml) for ml in move_lines_vals],
        }

        if self.dry_run:
            tot = sum(ml['price_unit'] for ml in move_lines_vals)
            consumed_str = '; '.join(
                f"POL {p['po_line_id']} on {p['oda_name']} "
                f"({p['voce']}/{p['cls']}, EUR{p['old_price_unit']:.2f}->EUR{p['new_price']:.2f}, "
                f"tax{p['new_tax_id']}, acc{p['account_id']}, targa={p['targa'] or '?'})"
                for p in consumed_pol_info)
            logger.info(
                f"[DRY_RUN] create_bozza_automezzi vat={vat} fornitore={nome_forn} "
                f"OdA={all_odas} righe={len(move_lines_vals)} "
                f"consume-POL=[{consumed_str}] "
                f"tot_imponibile={tot:.2f}")
            primary = consumed_pol_info[0]
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': p['voce']}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(success=True, action='create_draft_automezzi',
                               move_id=None, dry_run=True,
                               po_line_id=primary['po_line_id'],
                               old_price_unit=primary['old_price_unit'],
                               old_name=primary['old_name'],
                               old_date_planned=primary['old_date_planned'],
                               extra_po_lines=extras or None)

        # === SCRITTURA REALE ===
        try:
            for p in consumed_pol_info:
                self.client._call('purchase.order.line', 'write',
                    [p['po_line_id']], {
                        'price_unit': p['new_price'],
                        'name': p['new_name'],
                        'taxes_id': [(6, 0, [p['new_tax_id']])],
                        'product_qty': 1,
                        'qty_received': 1,
                        'qty_received_manual': 1,
                        'date_planned': invoice_date,
                    })
                logger.info(f"Updated POL {p['po_line_id']} on {p['oda_name']} "
                           f"({p['voce']}/{p['cls']}): price={p['new_price']:.2f}")

            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            if not move_id:
                raise RuntimeError("create move returned empty")
            logger.info(f"Created Automezzi move {move_id} vat={vat} OdA={all_odas} "
                       f"consume-POL ids={[p['po_line_id'] for p in consumed_pol_info]}")

            # Allego XML
            if analysis.raw_xml:
                try:
                    self.client._call('ir.attachment', 'create', {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    })
                except Exception as e:
                    logger.warning(f"Allegato XML fallito: {e}")

            # Marco fatturapa registered
            if analysis.attachment_id:
                try:
                    self.client._call('account.move', 'write', [move_id], {
                        'fatturapa_attachment_in_id': analysis.attachment_id,
                    })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito: {e}")

            primary = consumed_pol_info[0]
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': p['voce']}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(success=True, action='create_draft_automezzi',
                               move_id=move_id, dry_run=False,
                               po_line_id=primary['po_line_id'],
                               old_price_unit=primary['old_price_unit'],
                               old_name=primary['old_name'],
                               old_date_planned=primary['old_date_planned'],
                               extra_po_lines=extras or None)
        except Exception as e:
            logger.exception("Errore create_bozza_automezzi")
            for p in consumed_pol_info:
                try:
                    self.client._call('purchase.order.line', 'write',
                        [p['po_line_id']], {
                            'price_unit': p['old_price_unit'],
                            'name': p['old_name'],
                            'taxes_id': [(6, 0, p['old_taxes_id'])],
                            'qty_received': 0,
                            'qty_received_manual': 0,
                        })
                except Exception:
                    logger.warning(f"Restore POL {p['po_line_id']} fallito")
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message=str(e), dry_run=False)

    # === Telepass canoni === #

    @staticmethod
    def _classify_voce_telepass(desc: str) -> str:
        """Identifica la voce di una riga XML Telepass dalla descrizione.

        Ritorna uno di: 'canone' | 'parcheggio' | 'bollo' | 'quota_associativa'.
        Default 'canone' (caso più frequente).
        """
        d = (desc or '').upper()
        if 'BOLLO' in d:
            return 'bollo'
        if 'QUOTA ASSOCIATIVA' in d:
            return 'quota_associativa'
        if 'PARCHEGG' in d:
            return 'parcheggio'
        return 'canone'

    def create_bozza_telepass_canoni(self, analysis,
                                       mapping_entry: Dict) -> WriteResult:
        """Crea bozza per fattura canoni Telepass S.p.A. (IT09771701001).

        Pattern consume-POL multi-line con riscrittura totale POL "TEST €1
        generic" pre-create da Acquisti su P03722.

        Per ogni riga XML:
          1. Identifica voce (canone/parcheggio/bollo/quota_associativa)
          2. Determina conto contabile dalla mappatura
             (canone -> 1124, parcheggio -> 368, bollo/quota -> 160/1124)
          3. Tax_id letto dall'XML (aliquota 22% -> 11, 0% bollo -> 47,
             0% esente -> 54). Override per voci speciali via lookup.
          4. Prende prima POL libera P03722 (FIFO id), riscrive
             completamente: name, price_unit, taxes_id, qty_received=1
          5. Crea move_line con purchase_line_id collegato

        Rollback: ripristina POL ai valori originali (TEST/€1/tax=11/qty=0)
        via extra_po_lines_json (schema DB già esistente).

        Returns WriteResult con po_line_id (1ª POL) + extra_po_lines (2ª-Nª).
        """
        if not analysis.xml_data:
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01',):
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message=f"Tipo {tipo_doc} non supportato "
                                             f"per Telepass canoni (atteso TD01)",
                               dry_run=self.dry_run)

        cc = getattr(analysis.xml_data, 'codice_cliente', None)
        if not cc:
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message="codice_cliente mancante in XML",
                               dry_run=self.dry_run)

        cc_type = mapping_entry.get('cc_type')
        oda_name = mapping_entry.get('oda_fisso')
        if cc_type != 'telepass_main' or not oda_name:
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message=(
                                   f"mapping_entry incompleto: cc_type={cc_type}, "
                                   f"oda={oda_name}"),
                               dry_run=self.dry_run)

        # Cerca OdA P03722 su Ecotel
        ECOTEL = 1
        pos = self.client._call('purchase.order', 'search_read',
            [('name', '=', oda_name), ('company_id', '=', ECOTEL)],
            fields=['id', 'name', 'state', 'partner_id', 'currency_id'],
            limit=1)
        if not pos:
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message=f"OdA {oda_name} non trovato su Ecotel",
                               dry_run=self.dry_run)
        po = pos[0]
        po_id = po['id']
        po_name = po['name']
        partner_id = (po['partner_id'][0]
                       if isinstance(po['partner_id'], list) else None)
        currency_id = (po['currency_id'][0]
                        if isinstance(po['currency_id'], list) else None)
        if not partner_id:
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message=f"OdA {oda_name} senza partner",
                               dry_run=self.dry_run)

        # Righe XML
        righe = analysis.xml_data.righe or []
        if not righe:
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message="Nessuna riga in XML",
                               dry_run=self.dry_run)

        # Date
        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        date_contabile = self._data_contabile(analysis, invoice_date)
        date_iva = self._end_of_month(invoice_date)

        # Conti dalla mappatura
        conto_canone = mapping_entry.get('conto_canone_id', 1124)
        conto_parcheggio = mapping_entry.get('conto_parcheggio_id', 368)
        conto_bollo = mapping_entry.get('conto_bollo_id', 160)
        # Map voce -> (conto, tax_default)
        # tax_default: usato come fallback se aliquota XML è 0 e voce
        # determina tax specifico (bollo->47, quota_associativa->54)
        VOCE_TO_CONTO = {
            'canone':            conto_canone,
            'parcheggio':        conto_parcheggio,
            'bollo':             conto_bollo,
            'quota_associativa': conto_canone,
        }
        VOCE_TO_TAX_FALLBACK = {
            'bollo':             47,    # N1 escluse art.15
            'quota_associativa': 54,    # N4 esenti
        }

        # Mappa aliquota XML -> tax_id Odoo (le 3 più comuni Telepass)
        ALIQUOTA_TO_TAX = {
            22.0: 11,    # 22% S
        }

        # Recupero TUTTE le POL libere di P03722 (criterio standard)
        libere = self._find_libere_purchase_order_lines(po_id, 'standard_qty_inv_rec')
        if len(libere) < len(righe):
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message=(
                                   f"POL libere insufficienti su {po_name}: "
                                   f"{len(libere)} disponibili, {len(righe)} servono. "
                                   f"Acquisti deve aggiungere altre POL TEST a {po_name}."),
                               dry_run=self.dry_run)

        # Prendo le prime N (FIFO id). Le libere sono già ordinate per
        # (price_unit ascending, id) → buona priorità.
        consumed_pol_info: List[Dict] = []
        move_lines_vals: List[Dict] = []
        for idx, riga in enumerate(righe):
            pol = libere[idx]
            voce = self._classify_voce_telepass(riga.descrizione)
            conto = VOCE_TO_CONTO[voce]
            # Tax_id: prima da aliquota XML, poi fallback per voci speciali
            tax_id = ALIQUOTA_TO_TAX.get(float(riga.aliquota_iva or 0))
            if tax_id is None:
                tax_id = VOCE_TO_TAX_FALLBACK.get(voce, 11)

            # Costruzione name 3-righe pattern utenti (verificato gen-mar 2026)
            # Voci cc-specific (canone/parcheggio): include "Codice cliente: <CC>"
            # Voci globali (bollo/quota_associativa): solo voce su 1 riga.
            desc_orig = (riga.descrizione or '').strip()
            if voce in ('bollo', 'quota_associativa'):
                new_name = desc_orig or voce.upper()
            else:
                new_name = f"{desc_orig}\nCodice cliente: {cc}"

            # Estraggo product/uom dalla POL (necessari per move_line via XML-RPC)
            _prod = pol.get('product_id')
            product_id = _prod[0] if isinstance(_prod, list) else _prod
            _uom = pol.get('product_uom')
            product_uom_id = _uom[0] if isinstance(_uom, list) else _uom
            _aa = pol.get('account_analytic_id')
            analytic_id = _aa[0] if isinstance(_aa, list) else _aa

            # Importo: prefer prezzo_totale (con sconti applicati) altrimenti
            # quantita * prezzo_unitario. Per Telepass canoni qty è sempre 1.
            price = float(riga.prezzo_totale or riga.prezzo_unitario or 0)

            consumed_pol_info.append({
                'po_line_id': pol['id'],
                'old_price_unit': pol.get('price_unit') or 0,
                'old_name': pol.get('name') or '',
                'old_date_planned': pol.get('date_planned') or None,
                'old_taxes_id': list(pol.get('taxes_id') or []),
                'voce': voce,
                'cls': voce,  # alias per coerenza con Autostrade extra_po_lines
                'new_price': round(price, 2),
                'new_name': new_name,
                'new_tax_id': tax_id,
                'account_id': conto,
                'product_id': product_id,
                'product_uom_id': product_uom_id,
                'analytic_account_id': analytic_id,
            })
            ml_vals = {
                'name': new_name,
                'account_id': conto,
                'price_unit': round(price, 2),
                'quantity': 1,
                'tax_ids': [(6, 0, [tax_id])],
                'purchase_line_id': pol['id'],
            }
            if product_id:
                ml_vals['product_id'] = product_id
            if product_uom_id:
                ml_vals['product_uom_id'] = product_uom_id
            if analytic_id:
                ml_vals['analytic_account_id'] = analytic_id
            move_lines_vals.append(ml_vals)

        # Vals move
        journal_id = mapping_entry.get('journal_id', 2)
        move_vals = {
            'move_type': 'in_invoice',
            'partner_id': partner_id,
            'invoice_date': invoice_date,
            'date': date_contabile,
            'l10n_it_vat_settlement_date': date_iva,
            'ref': invoice_number,
            'invoice_origin': po_name,
            'journal_id': journal_id,
            'company_id': ECOTEL,
            'currency_id': currency_id,
            'invoice_line_ids': [(0, 0, ml) for ml in move_lines_vals],
        }

        if self.dry_run:
            tot = sum(ml['price_unit'] for ml in move_lines_vals)
            consumed_str = ', '.join(
                f"POL {p['po_line_id']} ({p['voce']}, "
                f"€{p['old_price_unit']:.2f}->€{p['new_price']:.2f}, tx{p['new_tax_id']}, acc{p['account_id']})"
                for p in consumed_pol_info)
            logger.info(
                f"[DRY_RUN] create_bozza_telepass_canoni cc={cc} "
                f"OdA={po_name} righe={len(move_lines_vals)} "
                f"consume-POL=[{consumed_str}] "
                f"tot_imponibile={tot:.2f}")
            primary = consumed_pol_info[0]
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'cls': p['voce']}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(success=True,
                               action='create_draft_telepass_canoni',
                               move_id=None, dry_run=True,
                               po_line_id=primary['po_line_id'],
                               old_price_unit=primary['old_price_unit'],
                               old_name=primary['old_name'],
                               old_date_planned=primary['old_date_planned'],
                               extra_po_lines=extras or None)

        # === SCRITTURA REALE ===
        try:
            # Step 1: aggiorno tutte le POL consumate
            for p in consumed_pol_info:
                self.client._call('purchase.order.line', 'write',
                    [p['po_line_id']], {
                        'price_unit': p['new_price'],
                        'name': p['new_name'],
                        'taxes_id': [(6, 0, [p['new_tax_id']])],
                        'product_qty': 1,
                        'qty_received': 1,
                        'qty_received_manual': 1,
                        'date_planned': invoice_date,
                    })
                logger.info(f"Updated POL {p['po_line_id']} ({p['voce']}): "
                           f"price={p['new_price']:.2f}, tax={p['new_tax_id']}")

            # Step 2: creo account.move
            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            if not move_id:
                raise RuntimeError("create move returned empty")
            logger.info(f"Created Telepass move {move_id} cc={cc} OdA={po_name} "
                       f"consume-POL ids={[p['po_line_id'] for p in consumed_pol_info]}")

            # Allego XML al move
            if analysis.raw_xml:
                try:
                    self.client._call('ir.attachment', 'create', {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    })
                except Exception as e:
                    logger.warning(f"Allegato XML fallito (non blocca): {e}")

            # Marco fatturapa.attachment.in registered
            if analysis.attachment_id:
                try:
                    self.client._call('account.move', 'write', [move_id], {
                        'fatturapa_attachment_in_id': analysis.attachment_id,
                    })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito (non blocca): {e}")

            # Audit
            primary = consumed_pol_info[0]
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'cls': p['voce']}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(success=True,
                               action='create_draft_telepass_canoni',
                               move_id=move_id, dry_run=False,
                               po_line_id=primary['po_line_id'],
                               old_price_unit=primary['old_price_unit'],
                               old_name=primary['old_name'],
                               old_date_planned=primary['old_date_planned'],
                               extra_po_lines=extras or None)
        except Exception as e:
            logger.exception("Errore create_bozza_telepass_canoni")
            # Best-effort restore POL già aggiornate
            for p in consumed_pol_info:
                try:
                    self.client._call('purchase.order.line', 'write',
                        [p['po_line_id']], {
                            'price_unit': p['old_price_unit'],
                            'name': p['old_name'],
                            'taxes_id': [(6, 0, p['old_taxes_id'])],
                            'qty_received': 0,
                            'qty_received_manual': 0,
                        })
                except Exception:
                    logger.warning(f"Restore POL {p['po_line_id']} fallito")
            return WriteResult(success=False,
                               action='create_draft_telepass_canoni',
                               error_message=str(e), dry_run=False)

    # === Rollback === #

    def rollback_bozza(self, move_id: int, po_line_id: Optional[int] = None,
                        old_price_unit: Optional[float] = None,
                        old_name: Optional[str] = None,
                        old_date_planned: Optional[str] = None,
                        attachment_id: Optional[int] = None,
                        added_po_line_ids: Optional[List[int]] = None,
                        extra_po_lines: Optional[List[Dict]] = None) -> WriteResult:
        """
        Rollback di una bozza creata:
        1. Verifica che il move sia ancora in stato 'draft' (non cancella fatture posted!)
        2. Cancella il account.move
        3. Ripristina la purchase.order.line PRIMARIA al vecchio stato
           (prezzo, nome, data consegna, qty_received)
        4. Se passate extra_po_lines (consume-POL multi: Autostrade): ripristina
           anche le POL secondarie consumate al loro vecchio stato
           (price_unit/name/date_planned/qty_received).
        5. Se passate added_po_line_ids: rimuove le POL extra aggiunte all'OdA
           dal pattern MATCH_PARZIALE_OK + accessorie (spese trasporto/bolli).
        6. Se attachment_id passato, lo de-registra (registered=False)
           per riportare la fattura nel contenitore "e-fatture in ingresso"

        Rifiuta rollback su fatture posted per sicurezza.
        """
        if self.dry_run:
            extra_ids = [p.get('po_line_id') for p in (extra_po_lines or [])]
            logger.info(f"[DRY_RUN] rollback_bozza move_id={move_id}, "
                       f"po_line={po_line_id}, extra_pol={extra_ids}, "
                       f"added_pol={added_po_line_ids}")
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

            # Pulisco le POL extra aggiunte all'OdA in fase di create
            # (pattern MATCH_PARZIALE_OK + righe accessorie).
            # Strategia: prima tento unlink (funziona solo se OdA in draft);
            # se fallisce per stato purchase, annullo la riga (qty=0,
            # price=0, prefisso "[ANNULLATA]" nel nome). La POL rimane
            # visibile ma non incide sui totali OdA.
            if added_po_line_ids:
                # Recupero stato attuale (alcune potrebbero essere già
                # state rimosse, o l'OdA potrebbe essere già diverso)
                try:
                    existing_data = self.client._call(
                        'purchase.order.line', 'search_read',
                        [('id', 'in', list(added_po_line_ids))],
                        fields=['id', 'name'])
                except Exception as e:
                    logger.warning(f"Lettura POL extra {added_po_line_ids} "
                                   f"fallita (non blocca rollback): {e}")
                    existing_data = []

                for pol in existing_data:
                    pol_id = pol['id']
                    orig_name = pol.get('name', '')
                    try:
                        # Tento unlink (passa solo se OdA in draft)
                        self.client._call('purchase.order.line', 'unlink',
                                          [pol_id])
                        logger.info(f"Removed extra POL id={pol_id} "
                                   f"from OdA (rollback)")
                    except Exception:
                        # Fallback: azzero la POL (qty=0, price=0)
                        try:
                            new_name = orig_name
                            if not new_name.startswith('[ANNULLATA]'):
                                new_name = f"[ANNULLATA] {orig_name}"
                            self.client._call('purchase.order.line', 'write',
                                [pol_id], {
                                    'product_qty': 0,
                                    'price_unit': 0,
                                    'qty_received': 0,
                                    'qty_received_manual': 0,
                                    'name': new_name,
                                })
                            logger.info(f"Cancellata logicamente extra POL "
                                       f"id={pol_id} (qty=0, price=0, "
                                       f"prefix [ANNULLATA])")
                        except Exception as e2:
                            logger.warning(f"Azzeramento POL {pol_id} fallito "
                                           f"(non blocca rollback): {e2}")

            # De-registro l'attachment fatturapa (lo riporta nel contenitore
            # "e-fatture in ingresso")
            if attachment_id:
                try:
                    self.client._call('fatturapa.attachment.in', 'write',
                        [attachment_id], {'registered': False})
                    logger.info(f"De-registrato fatturapa.attachment.in {attachment_id}")
                except Exception as e:
                    logger.warning(f"De-registrazione attachment fallita (non blocca): {e}")

            # Ripristino la riga OdA se richiesto.
            # IMPORTANTE: qty_received va azzerato SOLO per servizi
            # (POL ledger di Trenitalia/Italo o servizi su OdA matchato).
            # Per le MERCI, qty_received riflette la ricezione magazzino reale
            # e NON va toccato dal rollback (altrimenti scolleghiamo la merce
            # ricevuta dalla picking di magazzino → bug visto su CEML5-M).
            if po_line_id:
                # Leggo tipo prodotto della POL per decidere
                prod_type = 'service'  # default conservativo
                try:
                    pol_data = self.client._call(
                        'purchase.order.line', 'read', [po_line_id],
                        fields=['id', 'product_id'])
                    if pol_data:
                        prod = pol_data[0].get('product_id')
                        prod_id = prod[0] if isinstance(prod, list) and prod else None
                        if prod_id:
                            prods = self.client._call(
                                'product.product', 'read', [prod_id],
                                fields=['id', 'type'])
                            if prods:
                                prod_type = prods[0].get('type') or 'service'
                except Exception as e:
                    logger.warning(f"Impossibile leggere tipo prodotto POL "
                                   f"{po_line_id}, fallback service: {e}")

                write_vals = {}
                if prod_type == 'service':
                    # Azzero qty_received per liberare la riga ledger
                    write_vals['qty_received'] = 0
                    write_vals['qty_received_manual'] = 0
                else:
                    # Merci: NON tocco qty_received (è gestito dal magazzino)
                    logger.info(f"PO line {po_line_id} è merce (type={prod_type}): "
                               f"qty_received NON azzerato dal rollback")

                if old_price_unit is not None:
                    write_vals['price_unit'] = old_price_unit
                if old_name is not None:
                    write_vals['name'] = old_name
                if old_date_planned:
                    write_vals['date_planned'] = old_date_planned

                if write_vals:
                    self.client._call('purchase.order.line', 'write',
                        [po_line_id], write_vals)
                    logger.info(f"Restored PO line {po_line_id} (type={prod_type}): "
                               f"vals={list(write_vals.keys())}")

            # Ripristino POL secondarie consumate (consume-POL multi:
            # Autostrade ecotel_main consuma 2 POL per fattura).
            if extra_po_lines:
                for ext in extra_po_lines:
                    ext_id = ext.get('po_line_id')
                    if not ext_id:
                        continue
                    ext_vals = {
                        'qty_received': 0,
                        'qty_received_manual': 0,
                    }
                    if ext.get('old_price_unit') is not None:
                        ext_vals['price_unit'] = ext['old_price_unit']
                    if ext.get('old_name') is not None:
                        ext_vals['name'] = ext['old_name']
                    if ext.get('old_date_planned'):
                        ext_vals['date_planned'] = ext['old_date_planned']
                    try:
                        self.client._call('purchase.order.line', 'write',
                            [ext_id], ext_vals)
                        logger.info(f"Restored extra PO line {ext_id} "
                                   f"({ext.get('cls','')}): "
                                   f"vals={list(ext_vals.keys())}")
                    except Exception as e:
                        logger.warning(f"Restore extra POL {ext_id} fallito: {e}")

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
            # Determino tipo prodotto per decidere se azzerare qty_received
            # (solo per servizi/POL ledger, mai per merci ricevute in magazzino)
            prod_type = 'service'  # default conservativo
            try:
                pol_data = self.client._call(
                    'purchase.order.line', 'read', [po_line_id],
                    fields=['id', 'product_id'])
                if pol_data:
                    prod = pol_data[0].get('product_id')
                    prod_id = prod[0] if isinstance(prod, list) and prod else None
                    if prod_id:
                        prods = self.client._call('product.product', 'read',
                            [prod_id], fields=['id', 'type'])
                        if prods:
                            prod_type = prods[0].get('type') or 'service'
            except Exception:
                pass

            write_vals = {}
            if prod_type == 'service':
                write_vals['qty_received'] = 0
                write_vals['qty_received_manual'] = 0
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
        data_contabile = self._data_contabile(analysis, invoice_date)
        data_competenza_iva = self._end_of_month(invoice_date)
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
            'date': data_contabile,
            'l10n_it_vat_settlement_date': data_competenza_iva,
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
