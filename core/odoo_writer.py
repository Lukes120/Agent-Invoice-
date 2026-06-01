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

    def _italian_month_from_iso(self, date_str: str) -> str:
        """
        Dato '2026-05-08' ritorna 'Maggio'.
        Usato per il routing POL di WE4SERVICES (P03696) dove le righe
        placeholder libere mensili hanno il nome del mese in italiano.
        """
        _MONTHS_IT = ['', 'Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio',
                      'Giugno', 'Luglio', 'Agosto', 'Settembre', 'Ottobre',
                      'Novembre', 'Dicembre']
        if not date_str or '-' not in date_str:
            return ''
        try:
            m = int(date_str.split('-')[1])
            return _MONTHS_IT[m] if 1 <= m <= 12 else ''
        except Exception:
            return ''

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

    def _find_po_line_by_keywords_all(self, libere, keywords):
        """
        Tra le righe libere, trova la prima la cui descrizione contiene
        TUTTE le keyword in AND (case-insensitive). Stabile sull'ordine
        in lista libere (già ordinata per id crescente nel reader): garantisce
        FIFO sulle POL gemelle (es. P03696 ha 2 POL identiche per mese/tipo).
        """
        kws = [k.lower() for k in keywords if k]
        if not kws:
            return None
        for l in libere:
            name = (l.get('name', '') or '').lower()
            if all(k in name for k in kws):
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
        elif mapping_entry.get('line_groups_by_month'):
            # WE4SERVICES P03696: routing 2D. Per ogni gruppo (Oneri/FEE)
            # raggruppo le righe XML che matchano la keyword tipo, poi
            # cerco la PO line LIBERA che contiene ENTRAMBE le keyword:
            # tipo (es. "Oneri Factoring") + mese italiano della data fattura
            # (es. "Maggio"). La tax usata è quella della POL libera stessa
            # (Oneri esente [54], FEE 22% [11]).
            month_label = self._italian_month_from_iso(invoice_date)
            if not month_label:
                return WriteResult(
                    success=False, action='create_draft',
                    error_message=f"Impossibile estrarre mese italiano da "
                                 f"data fattura '{invoice_date}'",
                    dry_run=self.dry_run)
            month_groups = mapping_entry['line_groups_by_month']
            month_assignments = self._match_lines_to_groups(main_lines, month_groups)
            desc_strategy = mapping_entry.get('description_strategy', '')
            for i, group_cfg in enumerate(month_groups):
                group_lines = month_assignments.get(i, [])
                if not group_lines:
                    continue
                amount = sum(r.prezzo_totale for r in group_lines)
                if amount == 0:
                    continue
                # Match AND: tipo + mese
                po_line = self._find_po_line_by_keywords_all(
                    libere, [group_cfg['match'], month_label])
                if not po_line:
                    return WriteResult(
                        success=False, action='create_draft',
                        error_message=f"Nessuna POL libera in {oda_name} "
                                     f"per '{group_cfg['match']}' mese '{month_label}' "
                                     f"(POL esaurite o mese non previsto)",
                        dry_run=self.dry_run)
                libere = [l for l in libere if l['id'] != po_line['id']]
                # Tax: usa quella della POL libera (già corretta in OdA)
                pl_taxes = po_line.get('taxes_id') or mapping_entry['taxes_id']
                # Descrizione: keep_original_with_ref → append numero fattura
                base_desc = po_line.get('name', '') or ''
                if desc_strategy == 'keep_original_with_ref' and invoice_number:
                    description = f"{base_desc} (rif.ft {invoice_number})"
                else:
                    description = base_desc
                assignments.append({
                    'po_line': po_line,
                    'amount': -amount if is_nota_credito else amount,
                    'move_amount': -amount if is_nota_credito else amount,
                    'description': description,
                    'taxes_id': list(pl_taxes),
                    'old_price': po_line.get('price_unit', 0),
                    'old_name': base_desc,
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
                           f"desc='{a['description'][:120]}', taxes={a['taxes_id']}")
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
        # PARZIALE_CUMULATIVO_OK incluso: apply_run_cumulative_check (P4) popola
        # partial_extra_lines quando l'eccesso del gruppo cumulativo e' spiegato
        # da accessorie. Stesso pattern, stesso writer-flow.
        is_partial_with_extras = (
            analysis.classification in ('MATCH_PARZIALE_OK', 'PARZIALE_CUMULATIVO_OK')
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
                and is_subset_suggested):
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
                logger.info(f"  ML: pl_id={ml.get('purchase_line_id')} "
                           f"acc={ml.get('account_id')} qty={ml.get('quantity',0):.2f} "
                           f"pu={ml.get('price_unit',0):.2f} tax={ml.get('tax_ids')} "
                           f"name={(ml.get('name') or '')[:60]!r}")
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
                # Lego qty_received al CUMULATO fatturato (qty_invoiced corrente +
                # delta di questa bozza) invece di sommare alla qty_received_manual
                # preesistente. Evita il doppio conteggio quando la POL servizio ha
                # gia' qty_received>0 impostata fuori dall'agent (es. Lyreco P04870
                # "spese" con ord=1/ric=1/fat=0 → col vecchio old+delta diventava
                # ric=2 > ordinato). Resta corretto per il pattern OdA-ledger:
                # 2ª fattura qty_invoiced=1 + delta=1 = 2, identico a prima.
                qty_inv_old = float(pl.get('qty_invoiced') or 0)
                new_rec_manual = qty_inv_old + delta
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
        # Servizi (specifico) — incluse spese notifica/gestione multe
        # (riaddebito noleggiatore: NLT non fattura mai la sanzione stessa,
        # solo il fee di notifica/gestione → conto 430230/430240)
        if any(tok in d for tok in (
                'GESTIONE E SERVIZI', 'CANONE SERVIZIO', 'CANONE SERVIZI',
                'CANONESERVIZIO',
                'NOTIFICA MULTA', 'NOTIFICA MULTE', 'NOTIFICA INFRAZIONE',
                'NOTIFICA INFRAZIONI', 'NOTIFICA SANZIONE', 'NOTIFICA SANZIONI',
                'GESTIONE MULTA', 'GESTIONE MULTE',
                'GESTIONE INFRAZIONE', 'GESTIONE INFRAZIONI',
                'GESTIONE SANZIONE', 'GESTIONE SANZIONI',
                'GESTIONE PRATICA MULTA', 'GESTIONE PRATICHE MULTA',
                'DIRITTI DI NOTIFICA', 'SPESE NOTIFICA',
                'RIADDEBITO MULTA', 'RIADDEBITO MULTE',
                'RIADDEBITO NOTIFICA', 'RIADDEBITO SANZION',
                'RIADDEBITO INFRAZION')):
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
    def _classify_voce_automezzi_full(riga) -> str:
        """Come `_classify_voce_automezzi` ma con fallback su aliquota IVA.

        Caso d'uso: Leasys (e simili) emettono righe XML con descrizione MUTA
        per le tasse di proprieta — solo targa+modello, es. 'GL898ZH PANDA 1.0
        FireFly 70cv'. Il classifier basato su keyword non puo' distinguere
        bollo da canone in questi casi.

        Indizio strutturale: i bolli sono N1 escluse art.15 (aliquota 0%),
        mentre i canoni sono al 22%. Se la desc cade nel default 'locazione'
        e l'aliquota XML e' 0%, sovrascrivo a 'tassa'.
        """
        desc = riga.descrizione or ''
        voce = OdooWriter._classify_voce_automezzi(desc)
        # Solo override quando classifier ha messo 'locazione' di default.
        # Le voci esplicite (tassa, servizi, spese_incasso) restano.
        if voce != 'locazione':
            return voce
        try:
            aliquota = float(getattr(riga, 'aliquota_iva', 0) or 0)
        except (TypeError, ValueError):
            return voce
        if aliquota == 0.0:
            return 'tassa'
        return voce

    @staticmethod
    def _classify_cls_from_pol_name(pol_name: str) -> Optional[str]:
        """Estrae classificazione fiscale veicolo dal `name` della POL.

        Le POL pre-pianificate da Acquisti sull'OdA-ledger annuale (es. P03021
        Leasys) hanno name semantico tipo:
          'P03021: Riaddebito Tassa Automobilistica Regionale GENNAIO 2026 USO PROMISCUO'
          'P03021: Riaddebito Tassa Automobilistica Regionale Furgoni febbraio 2026'
          'P03021: CANONE LOCAZIONE HC444CS 3008 25/02/26-31/03/26'

        Le varianti lessicali (raccolte da name reali registrati a mano dalla
        contabilita') sono:
          POOL (100%):           AUTOMEZZI, FURGONI, automezzi, POOL
          uso_promiscuo (70%):   USO PROMISCUO, AUTOVETTURE, autovetture, uso promiscuo
          super_lusso:           SUPER LUSSO, SUPERLUSSO

        Usato in `create_bozza_automezzi` come **fallback** quando la cls
        derivata da targa risulta 'default_unknown' (es. riga bollo aggregato
        senza targa nella descrizione XML).

        Ritorna None se nessuna etichetta riconoscibile -> il caller deve
        mantenere il fallback esistente (uso_promiscuo conservativo).
        """
        if not pol_name:
            return None
        name_u = pol_name.upper()
        if 'SUPER LUSSO' in name_u or 'SUPERLUSSO' in name_u:
            return 'super_lusso'
        # POOL: AUTOMEZZI/FURGONI. Attenzione: 'AUTOMEZZI' contiene 'AUTO'
        # ma NON va confuso con 'AUTOVETTURE'. Match esatto su token.
        if any(tok in name_u for tok in (
                ' AUTOMEZZI', 'AUTOMEZZI ', 'AUTOMEZZI\n', '\nAUTOMEZZI',
                'FURGONI', ' POOL', 'POOL ', 'POOL\n', '\nPOOL')):
            return 'POOL'
        # uso_promiscuo: USO PROMISCUO o AUTOVETTURE (femminile plurale, no
        # confusione con AUTOMEZZI)
        if 'USO PROMISCUO' in name_u or 'AUTOVETTURE' in name_u or 'AUTOVETTURA' in name_u:
            return 'uso_promiscuo'
        return None

    @staticmethod
    def _pol_name_matches_voce(pol: Dict, voce: str) -> bool:
        """Filtra POL adatte a una voce in base al `name` della POL.

        Le POL pre-pianificate da Acquisti su P03021 Leasys hanno name semantico
        che dichiara la voce (tassa/locazione/servizi). Quando la fattura XML
        e' "muta" sulla descrizione (solo targa+modello), questo filtro evita
        che una riga "bollo" (voce='tassa' dedotta da aliquota 0%) finisca su
        una POL "CANONE LOCAZIONE" semplicemente perche' era prima nell'ordine
        FIFO.

        Logica:
          - POL name contiene keyword tassa  -> serve solo voce='tassa'
          - POL name contiene 'CANONE LOCAZIONE' -> serve solo voce='locazione'
          - POL name contiene 'CANONE SERVIZI'/'CANONESERVIZIO' o keyword
            servizi/multe/penali -> serve solo voce='servizi'
          - POL name generico (no pattern) -> accetta qualsiasi voce (jolly).

        Funziona in AND col filtro product (`_pol_product_matches_voce`).
        """
        name_u = (pol.get('name') or '').upper()
        if not name_u:
            return True
        is_tassa_pol = any(t in name_u for t in (
            'TASSA AUTOMOBILISTICA', 'RIADDEBITO TASS', 'TASSA DI POSSESSO',
            'TASSA DI PROPRIETA', 'SUPERBOLLO',
            # 'BOLLO' da solo e' troppo generico (matcha 'BOLLO IN FATTURA'
            # che e' bollo, ok; ma non matcha falsi positivi nel dominio)
            'BOLLO',
        ))
        is_locazione_pol = (
            'CANONE LOCAZIONE' in name_u
            or 'CANONE LOC.' in name_u
            or 'CANONE NOLEGGIO' in name_u
            # 'LOCAZIONE' da sola, ma NON se preceduta da 'CANONE SERVIZIO LOCAZIONE'
            or (' LOCAZIONE ' in name_u and 'SERVIZIO' not in name_u)
        )
        is_servizi_pol = any(t in name_u for t in (
            'CANONE SERVIZIO', 'CANONE SERVIZI', 'CANONESERVIZIO',
            'GESTIONE E SERVIZI',
            'NOTIFICA VERBALI', 'NOTIFICA MULTA', 'NOTIFICA INFRAZIONE',
            'GESTIONE MULTA', 'GESTIONE INFRAZIONE',
            'PENALE', 'ADDEBITO SPESE AMM', 'SPESE AMMINISTRATIVE',
            'VERBALE',
        ))
        if voce == 'tassa':
            return is_tassa_pol or not (is_locazione_pol or is_servizi_pol)
        if voce == 'locazione':
            return is_locazione_pol or not (is_tassa_pol or is_servizi_pol)
        if voce == 'servizi':
            return is_servizi_pol or not (is_tassa_pol or is_locazione_pol)
        # spese_incasso e altre voci: gestione dal product matching
        return True

    @staticmethod
    def _pol_name_matches_periodo(pol: Dict, periodo: str) -> bool:
        """Filtra POL il cui name contiene il periodo italiano richiesto.

        `periodo` formato 'Maggio 2026' (output di _format_periodo_italiano).
        Match case-insensitive su mese+anno per evitare di consumare la POL
        del mese sbagliato (caso Athlon HD446BE: Luglio libera non deve essere
        usata per riga Maggio).

        Se `periodo` non passato (riga senza Mese A/Mese da), ritorna True
        (jolly). Se POL ha name SENZA alcun mese italiano, ritorna True (POL
        generica, ok per qualsiasi periodo).
        """
        if not periodo:
            return True
        name_u = (pol.get('name') or '').upper()
        if not name_u:
            return True
        # Verifica se la POL cita un mese italiano qualsiasi
        any_month_pattern = r'\b(' + '|'.join(
            OdooWriter._MESI_ITALIANI.values()).upper() + r')\b'
        has_any_month = re.search(any_month_pattern, name_u)
        if not has_any_month:
            return True  # POL generica
        return periodo.upper() in name_u

    @staticmethod
    def _pol_name_matches_targa(pol: Dict, targa: str) -> bool:
        """Filtra POL che citano una targa specifica nel `name`.

        Simmetrico a `_pol_name_matches_voce`: evita che una riga di una targa
        finisca su una POL pre-pianificata per un'altra targa (caso Athlon
        P04797 con sole POL HD446BE che non devono essere riscritte da una
        riga GW950EK).

        Se `targa` non passata, ritorna True (jolly). Se POL ha name senza
        nessuna targa (es. "TEST EUR1"), ritorna True (POL generica).
        """
        if not targa:
            return True
        name_u = (pol.get('name') or '').upper()
        if not name_u:
            return True
        # Controlla se la POL cita qualche targa
        any_targa = re.search(r'\b[A-Z]{2}\d{3}[A-Z]{2}\b', name_u)
        if not any_targa:
            return True  # POL generica, accetta
        return targa.upper() in name_u

    @staticmethod
    def _extract_modello_veicolo_from_riga(riga) -> str:
        """Estrae il modello veicolo da AltriDatiGestionali (Athlon).

        Athlon XML: <TipoDato>Veicolo</TipoDato><RiferimentoTesto>BMW X4...</RiferimentoTesto>.
        Fallback: prima parola lunga della descrizione XML.
        """
        adg = getattr(riga, 'altri_dati_gestionali', None) or {}
        veicolo = (adg.get('VEICOLO') or adg.get('Veicolo')
                    or adg.get('MODELLO') or '').strip()
        if veicolo:
            return veicolo
        return ''

    _MESI_ITALIANI = {
        1: 'Gennaio', 2: 'Febbraio', 3: 'Marzo', 4: 'Aprile',
        5: 'Maggio', 6: 'Giugno', 7: 'Luglio', 8: 'Agosto',
        9: 'Settembre', 10: 'Ottobre', 11: 'Novembre', 12: 'Dicembre',
    }

    @staticmethod
    def _format_periodo_italiano(data_str: str) -> str:
        """Converte '2026-05-01' o '2026-05-31' in 'Maggio 2026'."""
        if not data_str:
            return ''
        try:
            from datetime import datetime
            d = datetime.strptime(data_str[:10], '%Y-%m-%d')
            return f"{OdooWriter._MESI_ITALIANI[d.month]} {d.year}"
        except Exception:
            return ''

    @staticmethod
    def _extract_periodo_from_riga(riga) -> str:
        """Periodo di competenza Athlon-style 'Maggio 2026'.

        Prende 'Mese a' da AltriDatiGestionali, fallback su 'Mese da'.
        """
        adg = getattr(riga, 'altri_dati_gestionali', None) or {}
        mese_a = adg.get('MESE A') or adg.get('Mese a') or ''
        mese_da = adg.get('MESE DA') or adg.get('Mese da') or ''
        ref = mese_a or mese_da
        return OdooWriter._format_periodo_italiano(ref)

    @staticmethod
    def _build_athlon_pol_name(modello: str, targa: str, periodo: str) -> str:
        """Costruisce name POL Athlon nel formato storico:
            'Noleggio Mercedes-Benz GLE Coupé\nTarga: HD446BE\nPeriodo: Maggio 2026'
        """
        if not modello:
            modello = 'veicolo'
        parts = [f"Noleggio {modello}"]
        if targa:
            parts.append(f"Targa: {targa}")
        if periodo:
            parts.append(f"Periodo: {periodo}")
        return "\n".join(parts)

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

    _VIRTUAL_POL_COUNTER = [-1]  # contatore placeholder id (negativi)

    def _build_virtual_pol_athlon(self, ri: Dict, mapping_entry: Dict,
                                     po_id: int, existing_libere: List[Dict],
                                     invoice_date: str) -> Optional[Dict]:
        """Costruisce una POL "virtuale" per Athlon quando manca la POL del periodo.

        Eredita product_id / product_uom / account_analytic dalle POL gemelle
        della stessa targa già presenti sull'OdA (sia libere sia usate).
        Il `name` segue il pattern storico Athlon:
            "Noleggio {Veicolo XML}\nTarga: {targa}\nPeriodo: {Mese A 'Italiano' YYYY}"

        Ritorna un dict POL-like (campi minimi richiesti dal loop chiamante)
        con placeholder_id negativo. La scrittura reale (create POL su Odoo)
        avviene poi in batch.
        """
        riga = ri['riga']
        targa = ri['targa'] or ''
        periodo = self._extract_periodo_from_riga(riga) or self._format_periodo_italiano(invoice_date)

        # Cerca POL gemelle (stessa targa, già su quest'OdA) per ereditare
        # product_id, product_uom, analytic_account E per copiare il modello
        # formattato (es. "Mercedes-Benz GLE Coupé" invece di XML grezzo
        # "MERCEDESBENZ GLE COUPE GLE 350 de 4M EQ AMG Line Prem Plus").
        sibling = None
        modello_sibling = ''
        if targa:
            siblings = self.client._call('purchase.order.line', 'search_read',
                [('order_id', '=', po_id),
                 ('name', 'ilike', targa)],
                fields=['id', 'name', 'product_id', 'product_uom',
                         'account_analytic_id', 'price_unit', 'taxes_id'],
                limit=1)
            if siblings:
                sibling = siblings[0]
                s_name = sibling.get('name') or ''
                if s_name.startswith('Noleggio '):
                    first_line = s_name.split('\n')[0]
                    modello_sibling = first_line.replace('Noleggio ', '', 1).strip()
        if not sibling:
            # Fallback: prima POL libera dell'OdA (per product/uom)
            if existing_libere:
                sibling = existing_libere[0]
        if not sibling:
            return None

        # Modello: preferisco sibling (formato pulito), fallback su XML
        modello = modello_sibling or self._extract_modello_veicolo_from_riga(riga)
        new_name = self._build_athlon_pol_name(modello, targa, periodo)

        price = float(riga.prezzo_totale or riga.prezzo_unitario or 0)
        OdooWriter._VIRTUAL_POL_COUNTER[0] -= 1
        placeholder_id = OdooWriter._VIRTUAL_POL_COUNTER[0]
        return {
            'id': placeholder_id,  # negativo = placeholder, sostituito alla scrittura
            'name': new_name,
            'price_unit': price,
            'product_id': sibling.get('product_id'),
            'product_uom': sibling.get('product_uom'),
            'account_analytic_id': sibling.get('account_analytic_id'),
            'taxes_id': [ri['tax_id']],
            'qty_invoiced': 0.0,
            'qty_received': 0.0,
            'product_qty': 1.0,
            'date_planned': invoice_date,
            '_is_virtual': True,
        }

    def _compute_athlon_gap_fillers(self, pol_extra_to_create: List[Dict],
                                       oda_state: Dict, oda_to_righe: Dict) -> List[Dict]:
        """Calcola POL libere da creare per riempire i buchi tra le POL
        virtuali appena consumate e le POL pre-pianificate successive sulla
        stessa targa+OdA.

        Esempio Athlon HD446BE su P04797:
          POL esistenti per targa: Aprile(usata), Luglio(libera), Agosto,
          Settembre, Ottobre, Novembre, Dicembre.
          POL virtuale appena creata per la fattura: Maggio.
          Gap da riempire: Giugno (1 POL libera).
        """
        gap_fillers = []
        # Mesi italiani -> index (invertito per parsing)
        month_to_idx = {v.upper(): k for k, v in OdooWriter._MESI_ITALIANI.items()}
        month_re = re.compile(r'\b(' + '|'.join(OdooWriter._MESI_ITALIANI.values()).upper()
                                + r')\s+(\d{4})\b')

        for pv in pol_extra_to_create:
            targa = pv['targa']
            po_id = pv['po_id']
            if not targa:
                continue
            # Parse mese della POL virtuale dal name
            m = month_re.search(pv['name'].upper())
            if not m:
                continue
            virtual_month = month_to_idx[m.group(1)]
            virtual_year = int(m.group(2))
            virtual_ym = (virtual_year, virtual_month)
            # Cerca tutte le POL della targa sull'OdA
            siblings = self.client._call('purchase.order.line', 'search_read',
                [('order_id', '=', po_id),
                 ('name', 'ilike', targa)],
                fields=['id', 'name', 'price_unit', 'product_id',
                         'product_uom', 'taxes_id', 'date_planned'])
            # Trova il prossimo mese pianificato dopo virtual_ym
            future_months = []
            sibling_template = None
            for s in siblings:
                m2 = month_re.search((s.get('name') or '').upper())
                if not m2:
                    continue
                sy = (int(m2.group(2)), month_to_idx[m2.group(1)])
                if sy > virtual_ym:
                    future_months.append(sy)
                if sibling_template is None:
                    sibling_template = s
            if not future_months:
                continue
            future_months.sort()
            next_ym = future_months[0]
            # Genero mesi nel gap (esclusivi virtual_ym, esclusivi next_ym)
            cur = virtual_ym
            while True:
                # Avanza di 1 mese
                y, mo = cur
                mo += 1
                if mo > 12:
                    mo = 1
                    y += 1
                cur = (y, mo)
                if cur >= next_ym:
                    break
                # Crea POL gap-filler
                mese_str = OdooWriter._MESI_ITALIANI[mo]
                modello = ''
                # Estrai modello dal name della sibling template
                if sibling_template:
                    s_name = sibling_template.get('name') or ''
                    if s_name.startswith('Noleggio '):
                        first_line = s_name.split('\n')[0]
                        modello = first_line.replace('Noleggio ', '', 1).strip()
                gap_name = self._build_athlon_pol_name(
                    modello or 'veicolo', targa, f"{mese_str} {y}")
                # Tax: riusa quella della POL virtuale (cls corrente dal PARCO)
                tax_id_gap = pv['tax_id']
                # Product: dal sibling
                prod = (sibling_template.get('product_id')
                         if sibling_template else None)
                uom = (sibling_template.get('product_uom')
                        if sibling_template else None)
                gap_fillers.append({
                    'po_id': po_id,
                    'oda_name': pv['oda_name'],
                    'name': gap_name,
                    'price_unit': sibling_template.get('price_unit') or pv['price_unit'],
                    'tax_id': tax_id_gap,
                    'product_id': prod[0] if isinstance(prod, list) else prod,
                    'product_uom': uom[0] if isinstance(uom, list) else uom,
                    'targa': targa,
                    'date_planned': f"{y}-{mo:02d}-01",
                })
        return gap_fillers

    def _discover_oda_with_targa_history(self, partner_id: int, targa: str,
                                            candidates: List[Optional[str]],
                                            company_id: int = 1) -> Optional[str]:
        """Variante di _auto_discover_oda_by_targa che accetta anche POL già
        USATE (qty_invoiced>0) per la targa.

        Usato quando una targa ha tutte le POL pre-pianificate già consumate,
        ma serve comunque scegliere l'OdA giusto per creare POL extra
        (auto_create_missing_pol). Restringe la ricerca a `candidates` se
        fornito.

        Restituisce il `name` dell'OdA o None.
        """
        if not partner_id or not targa:
            return None
        cand_filter = [c for c in candidates if c]
        if cand_filter:
            pos = self.client._call('purchase.order', 'search_read',
                [('partner_id', '=', partner_id),
                 ('company_id', '=', company_id),
                 ('state', '=', 'purchase'),
                 ('name', 'in', cand_filter)],
                fields=['id', 'name'], limit=20)
        else:
            pos = self.client._call('purchase.order', 'search_read',
                [('partner_id', '=', partner_id),
                 ('company_id', '=', company_id),
                 ('state', '=', 'purchase')],
                fields=['id', 'name'], limit=50)
        ranked = []
        for po in pos:
            cnt = self.client._call('purchase.order.line', 'search_count',
                [('order_id', '=', po['id']),
                 ('name', 'ilike', targa)])
            if cnt > 0:
                ranked.append((po['name'], cnt))
        if not ranked:
            return None
        ranked.sort(key=lambda x: -x[1])
        return ranked[0][0]

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

        # Dispatch Leasys aggregato: per IT06714021000 si usa il pattern
        # "4 POL canoni aggregate + N POL extra ad-hoc" anziché "1 POL per riga"
        # (deciso 13/05/2026 — vedi memoria project_session_2026_05_13_leasys_refactor).
        if vat == 'IT06714021000':
            return self._create_bozza_leasys_aggregated(analysis, mapping_entry)

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
            # Classifier voce con fallback su aliquota IVA per descrizioni XML
            # mute (es. Leasys bollo righe 'TARGA MODELLO' senza keyword).
            voce = self._classify_voce_automezzi_full(riga)
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
            elif mapping_entry.get('multi_oda_per_targa') and targa:
                # Pattern Athlon: 1 OdA per veicolo (P03798=GW950EK,
                # P04797=HD446BE). Per ogni riga, cerca l'OdA con POL
                # (libere o usate) che cita la targa fra oda_fisso + oda_storico
                # + auto-discovery globale del fornitore.
                oda_name = self._auto_discover_oda_by_targa(
                    mapping_entry.get('partner_id'),
                    targa,
                    company_id=mapping_entry.get('company_id', 1))
                if not oda_name:
                    # Cerca anche in OdA aperti con POL già usate per la targa
                    # (caso: tutte le POL del veicolo già consumate, ma OdA
                    # ancora pertinente per creare POL extra).
                    oda_name = self._discover_oda_with_targa_history(
                        mapping_entry.get('partner_id'),
                        targa,
                        candidates=[mapping_entry.get('oda_fisso'),
                                    mapping_entry.get('oda_storico')],
                        company_id=mapping_entry.get('company_id', 1))
                if not oda_name:
                    oda_name = mapping_entry.get('oda_fisso')
                if oda_name:
                    logger.info(f"Multi-OdA Athlon: targa={targa} -> {oda_name}")
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

        # POL extra da creare al volo (per fornitori con auto_create_missing_pol)
        # come Athlon, quando per (targa, periodo) non esiste POL pre-pianificata.
        pol_extra_to_create = []  # list di dict: {'po_id', 'oda_name', 'name', 'price_unit', 'tax_id', 'product_id', 'targa', 'is_gap_filler'}

        for ri in riga_info_list:
            oda_name = ri['oda_name']
            st = oda_state[oda_name]
            voce = ri['voce']
            targa = ri['targa'] or ''
            periodo = self._extract_periodo_from_riga(ri['riga'])
            # Match per periodo attivo solo se mapping ha auto_create_missing_pol:
            # senza creazione automatica, useremmo POL del mese sbagliato come
            # fallback. Con creazione automatica, preferiamo POL nuova al periodo
            # giusto.
            match_periodo = bool(mapping_entry.get('auto_create_missing_pol')) and periodo
            # Cerca prima POL libera matching: targa + periodo + product + name semantico.
            # Importante per Athlon multi-veicolo (P04797 ha solo POL HD446BE,
            # P03798 solo GW950EK) e per fatture che coprono mesi diversi dalle
            # POL pre-pianificate.
            pol = None
            for cand in st['libere']:
                if cand['id'] in oda_pol_used[oda_name]:
                    continue
                if (self._pol_name_matches_targa(cand, targa)
                        and (not match_periodo or self._pol_name_matches_periodo(cand, periodo))
                        and self._pol_product_matches_voce(cand, voce)
                        and self._pol_name_matches_voce(cand, voce)):
                    pol = cand
                    break
            # Fallback livello 1: rilasso il filtro voce/name (POL generiche
            # tipo 'TEST EUR1'), ma mantengo filtro targa + periodo.
            if pol is None:
                for cand in st['libere']:
                    if cand['id'] in oda_pol_used[oda_name]:
                        continue
                    if (self._pol_name_matches_targa(cand, targa)
                            and (not match_periodo or self._pol_name_matches_periodo(cand, periodo))
                            and self._pol_product_matches_voce(cand, voce)):
                        pol = cand
                        break
            # Fallback livello 2: prima libera con targa matching (anche
            # product non matching, fornitori con product unico). Filtro
            # periodo ancora attivo se richiesto.
            if pol is None:
                for cand in st['libere']:
                    if cand['id'] in oda_pol_used[oda_name]:
                        continue
                    if (self._pol_name_matches_targa(cand, targa)
                            and (not match_periodo or self._pol_name_matches_periodo(cand, periodo))):
                        pol = cand
                        break
            # Fallback livello 3: prima libera in assoluto (solo se NON c'è
            # auto_create — altrimenti meglio creare POL nuova che riusare
            # POL di altra targa).
            if pol is None and not mapping_entry.get('auto_create_missing_pol'):
                for cand in st['libere']:
                    if cand['id'] not in oda_pol_used[oda_name]:
                        pol = cand
                        break
            # Auto-create: se nessuna POL libera per la targa e mapping
            # consente la creazione, ne creiamo una al volo (pattern Athlon
            # quando manca la POL del periodo richiesto).
            if pol is None and mapping_entry.get('auto_create_missing_pol'):
                pol = self._build_virtual_pol_athlon(
                    ri=ri,
                    mapping_entry=mapping_entry,
                    po_id=st['po_id'],
                    existing_libere=st['libere'],
                    invoice_date=invoice_date)
                if pol:
                    # Aggiungo alla coda di POL da creare (uso un id negativo
                    # come placeholder; verra' assegnato un id reale alla
                    # scrittura).
                    pol_extra_to_create.append({
                        'placeholder_id': pol['id'],
                        'po_id': st['po_id'],
                        'oda_name': oda_name,
                        'name': pol['name'],
                        'price_unit': pol['price_unit'],
                        'tax_id': ri['tax_id'],
                        'product_id': (pol['product_id'][0]
                                        if isinstance(pol.get('product_id'), list)
                                        else pol.get('product_id')),
                        'product_uom': (pol['product_uom'][0]
                                         if isinstance(pol.get('product_uom'), list)
                                         else pol.get('product_uom')),
                        'targa': targa,
                        'is_gap_filler': False,
                    })
                    # La aggiungo alle libere dell'OdA per il prossimo ciclo
                    st['libere'].append(pol)
            if pol is None:
                return WriteResult(success=False, action='create_draft_automezzi',
                                   error_message=(f"POL libere insufficienti su {oda_name} "
                                                  f"per voce={voce} targa={targa or '?'} (fattura ha "
                                                  f"{len(oda_to_righe[oda_name])} righe)"),
                                   dry_run=self.dry_run)
            oda_pol_used[oda_name].add(pol['id'])

            # Override cls dal name POL se PARCO_BY_TARGA/CONTRATTO non
            # l'hanno determinata. Caso d'uso: bollo Leasys aggregato (no
            # targa nella desc XML) -> cls cade in 'default_unknown'/
            # 'default_no_mapping'. Il name della POL pre-pianificata da
            # Acquisti dichiara la classificazione ('USO PROMISCUO' vs
            # 'AUTOMEZZI/FURGONI').
            if ri['cls_source'] in ('default_unknown', 'default_no_mapping'):
                cls_from_pol = self._classify_cls_from_pol_name(pol.get('name'))
                if cls_from_pol and cls_from_pol != ri['cls']:
                    ri['cls'] = cls_from_pol
                    ri['cls_source'] = 'pol_name'
                    # Ricalcola conto + tax con la cls aggiornata
                    new_conto = (CONTO_AUTOMEZZI.get((voce, cls_from_pol))
                                 or CONTO_AUTOMEZZI.get((voce, 'uso_promiscuo')))
                    if new_conto:
                        ri['conto'] = new_conto
                    new_tax = (TAX_AUTOMEZZI.get(vat, {}).get((voce, cls_from_pol))
                               or TAX_AUTOMEZZI.get(vat, {}).get((voce, '*')))
                    if new_tax:
                        ri['tax_id'] = new_tax

            riga = ri['riga']
            # Importo riga
            price = float(riga.prezzo_totale or riga.prezzo_unitario or 0)
            # NC: prezzo negativo
            if is_nota_credito:
                price = -abs(price)

            # Strategia descrizione:
            #  - 'leasys' (e qualunque mapping con keep_pol_name=True):
            #      preserva il name della POL pre-pianificata da Acquisti,
            #      che e' gia' semantico (es. 'P03021: Riaddebito Tassa
            #      Automobilistica Regionale GENNAIO 2026 USO PROMISCUO').
            #      Fallback su desc XML se POL ha name generico ('TEST EUR1').
            #  - altri (default): usa la desc XML (Tecnoalt/Athlon/UnipolRental/
            #      ALD/Arval hanno desc XML informativa con targa+modello+
            #      periodo o keyword tassa esplicita).
            desc_strategy = (mapping_entry.get('description_strategy') or '').lower()
            keep_pol_name = (desc_strategy == 'leasys'
                              or mapping_entry.get('keep_pol_name') is True)
            desc_orig = (riga.descrizione or '').strip()
            pol_name = (pol.get('name') or '').strip()
            pol_name_is_semantic = (
                self._classify_cls_from_pol_name(pol_name) is not None
                or self._pol_name_matches_voce(pol, ri['voce']) and pol_name
                    and any(t in pol_name.upper() for t in (
                        'TASSA AUTOMOBILISTICA', 'RIADDEBITO TASS',
                        'CANONE LOCAZIONE', 'CANONE SERVIZIO',
                        'CANONE SERVIZI', 'CANONESERVIZIO', 'CANONE NOLEGGIO',
                        'NOLEGGIO'))
            )
            if keep_pol_name and pol_name_is_semantic:
                new_name = pol_name
            elif desc_orig:
                new_name = desc_orig
            else:
                new_name = f"{ri['voce']} automezzi"
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

        # Gap-filling: calcola POL libere future da creare per riempire i
        # buchi tra le POL virtuali appena consumate e le POL pre-pianificate
        # successive. Solo per fornitori con fill_pol_gap_until_next=True
        # (Athlon). Usa il pattern name "...Mese YYYY" italiano.
        pol_gap_fillers = []
        if mapping_entry.get('fill_pol_gap_until_next') and pol_extra_to_create:
            pol_gap_fillers = self._compute_athlon_gap_fillers(
                pol_extra_to_create, oda_state, oda_to_righe)

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
            if pol_extra_to_create:
                logger.info(
                    f"[DRY_RUN] POL virtuali da creare (consumate dalla fattura): "
                    + '; '.join(f"OdA={p['oda_name']} targa={p['targa']} "
                                f"price={p['price_unit']:.2f} tax{p['tax_id']} "
                                f"name={p['name']!r}"
                                for p in pol_extra_to_create))
            if pol_gap_fillers:
                logger.info(
                    f"[DRY_RUN] POL gap-filler libere da creare ({len(pol_gap_fillers)}): "
                    + '; '.join(f"OdA={p['oda_name']} targa={p['targa']} "
                                f"price={p['price_unit']:.2f} tax{p['tax_id']} "
                                f"name={p['name']!r}"
                                for p in pol_gap_fillers))
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
            # Step 0: crea POL virtuali (mapping placeholder_id<0 -> id reale Odoo)
            virtual_id_map = {}
            for pv in pol_extra_to_create:
                pol_vals = {
                    'order_id': pv['po_id'],
                    'name': pv['name'],
                    'price_unit': pv['price_unit'],
                    'product_qty': 1,
                    'taxes_id': [(6, 0, [pv['tax_id']])],
                    'date_planned': invoice_date,
                }
                if pv.get('product_id'):
                    pol_vals['product_id'] = pv['product_id']
                if pv.get('product_uom'):
                    pol_vals['product_uom'] = pv['product_uom']
                new_pol_id = self.client._call('purchase.order.line', 'create', pol_vals)
                if isinstance(new_pol_id, list):
                    new_pol_id = new_pol_id[0]
                virtual_id_map[pv['placeholder_id']] = new_pol_id
                logger.info(f"Created virtual POL {new_pol_id} on {pv['oda_name']} "
                           f"targa={pv['targa']} price={pv['price_unit']:.2f}: "
                           f"name={pv['name']!r}")
            # Sostituisco placeholder negativi con id reali in consumed_pol_info
            # e in move_lines_vals
            for p in consumed_pol_info:
                if p['po_line_id'] in virtual_id_map:
                    p['po_line_id'] = virtual_id_map[p['po_line_id']]
            for ml in move_lines_vals:
                if ml.get('purchase_line_id') in virtual_id_map:
                    ml['purchase_line_id'] = virtual_id_map[ml['purchase_line_id']]
            # Aggiorno anche move_vals (invoice_line_ids contiene tupla (0,0,ml))
            move_vals['invoice_line_ids'] = [(0, 0, ml) for ml in move_lines_vals]

            # Step 0b: crea POL gap-filler libere (qty_received=0)
            for pgf in pol_gap_fillers:
                gap_vals = {
                    'order_id': pgf['po_id'],
                    'name': pgf['name'],
                    'price_unit': pgf['price_unit'],
                    'product_qty': 1,
                    'taxes_id': [(6, 0, [pgf['tax_id']])],
                    'date_planned': pgf.get('date_planned') or invoice_date,
                }
                if pgf.get('product_id'):
                    gap_vals['product_id'] = pgf['product_id']
                if pgf.get('product_uom'):
                    gap_vals['product_uom'] = pgf['product_uom']
                new_gap_id = self.client._call('purchase.order.line', 'create', gap_vals)
                if isinstance(new_gap_id, list):
                    new_gap_id = new_gap_id[0]
                pgf['created_pol_id'] = new_gap_id
                logger.info(f"Created gap-filler POL {new_gap_id} on {pgf['oda_name']} "
                           f"targa={pgf['targa']}: name={pgf['name']!r}")

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

    # === Edenred UTA Mobility — carte carburante === #
    #
    # Routing fiscale Ecotel (verificato su move posted ACQ/2026/1898 e altre):
    #   classificazione    -> conto Odoo    + tax  + deducib.
    #   POOL (furgoni)        410300 id=358   11      100%
    #   uso_promiscuo         410410 id=1125  11       70%
    #   super_lusso           410400 id=359   73       20% (IVA indetraibile 60%)
    #   SERVIZIO              420190 id=1190  11      100% (servizi vari)
    _EDENRED_UTA_ROUTING = {
        'POOL':          {'account_id': 358,  'tax_id': 11, 'pol_keyword': 'furgoni'},
        'uso_promiscuo': {'account_id': 1125, 'tax_id': 11, 'pol_keyword': 'uso promiscuo'},
        'super_lusso':   {'account_id': 359,  'tax_id': 73, 'pol_keyword': 'uso amministratore'},
        'SERVIZIO':      {'account_id': 1190, 'tax_id': 11, 'pol_keyword': 'servizio'},
    }

    @staticmethod
    def _classify_carta_uta(numero_carta: str) -> Optional[str]:
        """Lookup classificazione carta UTA da config/carte_carburante_mapping.py.

        Ritorna 'POOL' / 'uso_promiscuo' / 'super_lusso' / 'SERVIZIO'
        oppure None se la carta non è in mappa (riga da censire).
        """
        if not numero_carta:
            return None
        try:
            from config.carte_carburante_mapping import get_classificazione_carta_uta
        except ImportError:
            return None
        return get_classificazione_carta_uta(str(numero_carta).strip())

    def create_bozza_edenred_uta(self, analysis,
                                    mapping_entry: Dict) -> WriteResult:
        """Crea bozza per fattura carte carburante Edenred UTA Mobility
        (IT01696270212).

        Pattern: ~114 righe XML di rifornimenti singoli → aggregazione per
        classificazione fiscale (POOL/uso_promiscuo/super_lusso/SERVIZIO)
        → max 4 voci move_line. Consume-POL multi-line su P03735 con POL
        pre-pianificate da Acquisti già semantiche (name "Costo carburante
        ft.n. XXX\n{classe}").

        Steps per ogni riga XML:
          1. Estrai numero_carta da <RiferimentoAmministrazione>
          2. Lookup classificazione (CARTE_UTA_BY_NUMERO)
             - Carta non trovata -> warning, fallback 'uso_promiscuo'
             - Riga senza RiferimentoAmministrazione -> 'SERVIZIO'
          3. Aggrega prezzo_totale per (classificazione)
        Steps di scrittura:
          1. Per ogni classificazione aggregata, cerca POL libera su P03735
             con name contenente la keyword (furgoni / uso promiscuo /
             uso amministratore / servizio)
          2. Riscrive POL: name="Costo carburante ft.n. {numero_fattura}\n{classe}"
             (preserva pattern storico), price_unit=totale_classe, taxes_id,
             qty_received=1
          3. Crea move_line per ciascuna classe con account_id, tax, qty=1,
             purchase_line_id collegato

        Returns WriteResult con po_line_id primaria + extra_po_lines (le altre).
        """
        if not analysis.xml_data:
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc != 'TD01':
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message=f"Tipo {tipo_doc} non supportato "
                                             f"(atteso TD01)",
                               dry_run=self.dry_run)

        vat = (analysis.xml_data.cedente_partita_iva or '').strip().upper()
        if vat != 'IT01696270212':
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message=f"P.IVA {vat} non Edenred UTA",
                               dry_run=self.dry_run)

        righe = analysis.xml_data.righe or []
        if not righe:
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message="Nessuna riga in XML",
                               dry_run=self.dry_run)

        nome_forn = mapping_entry.get('nome', 'Edenred UTA Mobility S.r.l.')
        oda_name = mapping_entry.get('oda_fisso', 'P03735')

        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        date_contabile = self._data_contabile(analysis, invoice_date)
        date_iva = self._end_of_month(invoice_date)

        # === Fase 1: aggrego per classificazione ===
        agg = {}   # cls -> {'totale': float, 'n_righe': int, 'carte': set, 'note_carte_missing': set}
        carte_non_mappate = set()
        righe_senza_carta = 0

        for riga in righe:
            numero_carta = ''
            adg = getattr(riga, 'altri_dati_gestionali', None) or {}
            # Provo a estrarre RiferimentoAmministrazione: il parser di solito
            # mette il valore in altri_dati_gestionali sotto chiave "TIPO_VALORE",
            # ma per Edenred UTA il riferimento amministrazione è un campo
            # diretto della riga (non un AltriDatiGestionali). Lo prendo da
            # un attributo del FatturaPALine se esiste.
            numero_carta = (
                getattr(riga, 'riferimento_amministrazione', '') or
                adg.get('RIFERIMENTO_AMMINISTRAZIONE') or
                adg.get('Riferimento_Amministrazione') or
                ''
            )
            numero_carta = str(numero_carta).strip()

            classif = None
            if numero_carta:
                classif = self._classify_carta_uta(numero_carta)
                if classif is None:
                    # Carta non mappata: fallback conservativo + segnalazione
                    carte_non_mappate.add(numero_carta)
                    classif = 'uso_promiscuo'
            else:
                # Riga senza riferimento carta = SERVIZIO (fee, gestione, ecc.)
                righe_senza_carta += 1
                classif = 'SERVIZIO'

            prezzo = float(getattr(riga, 'prezzo_totale', 0) or
                            getattr(riga, 'prezzo_unitario', 0) or 0)
            if classif not in agg:
                agg[classif] = {'totale': 0.0, 'n_righe': 0, 'carte': set()}
            agg[classif]['totale'] += prezzo
            agg[classif]['n_righe'] += 1
            if numero_carta:
                agg[classif]['carte'].add(numero_carta)

        if not agg:
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message="Nessuna riga aggregabile",
                               dry_run=self.dry_run)

        # === Fase 2: trova OdA + POL libere ===
        ECOTEL = 1
        pos = self.client._call('purchase.order', 'search_read',
            [('name', '=', oda_name), ('company_id', '=', ECOTEL)],
            fields=['id', 'name', 'state', 'partner_id', 'currency_id'],
            limit=1)
        if not pos:
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message=f"OdA {oda_name} non trovato su Ecotel",
                               dry_run=self.dry_run)
        po = pos[0]
        po_id = po['id']
        partner_id = (po['partner_id'][0]
                       if isinstance(po['partner_id'], list) else None)
        currency_id = (po['currency_id'][0]
                        if isinstance(po['currency_id'], list) else None)
        if not partner_id:
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message=f"OdA {oda_name} senza partner",
                               dry_run=self.dry_run)

        libere = self._find_libere_purchase_order_lines(po_id, 'standard_qty_inv_rec')
        if len(libere) < len(agg):
            return WriteResult(success=False,
                               action='create_draft_edenred_uta',
                               error_message=(
                                   f"POL libere insufficienti su {oda_name}: "
                                   f"{len(libere)} disponibili, {len(agg)} servono "
                                   f"(1 per classificazione: {list(agg.keys())}). "
                                   f"Acquisti deve aggiungere altre POL TEST a {oda_name}."),
                               dry_run=self.dry_run)

        # === Fase 3: match POL per classe via keyword nel name ===
        # Per ogni classe agg, cerco la prima POL libera il cui name contiene
        # la keyword associata (es. 'furgoni' per POOL).
        consumed_pol_info = []
        move_lines_vals = []
        used_pol_ids = set()

        # Preferisco ordine fisso (POOL, uso_promiscuo, super_lusso, SERVIZIO)
        # per stabilità in output e nei test.
        ORDER = ['POOL', 'uso_promiscuo', 'super_lusso', 'SERVIZIO']
        classi_ordinate = [c for c in ORDER if c in agg]

        for classif in classi_ordinate:
            info_cls = agg[classif]
            routing = self._EDENRED_UTA_ROUTING.get(classif)
            if not routing:
                return WriteResult(success=False,
                                   action='create_draft_edenred_uta',
                                   error_message=f"Routing non definito per classe {classif!r}",
                                   dry_run=self.dry_run)

            keyword = routing['pol_keyword'].lower()
            account_id = routing['account_id']
            tax_id = routing['tax_id']

            # Cerca POL libera con name contenente la keyword
            pol = None
            for cand in libere:
                if cand['id'] in used_pol_ids:
                    continue
                cand_name = (cand.get('name') or '').lower()
                if keyword in cand_name:
                    pol = cand
                    break
            # Fallback: prima POL libera disponibile (se nessuna matcha la keyword)
            if pol is None:
                for cand in libere:
                    if cand['id'] not in used_pol_ids:
                        pol = cand
                        break
            if pol is None:
                return WriteResult(success=False,
                                   action='create_draft_edenred_uta',
                                   error_message=(
                                       f"Nessuna POL libera per classe {classif!r} "
                                       f"(keyword '{keyword}') su {oda_name}"),
                                   dry_run=self.dry_run)
            used_pol_ids.add(pol['id'])

            # name move_line + POL: pattern storico Ecotel
            # "Costo carburante ft.n. {numero_fattura}\n{classe leggibile}"
            # Per SERVIZIO uso "Costo servizio \nft.n. {numero}"
            classe_label = {
                'POOL': 'furgoni',
                'uso_promiscuo': 'uso promiscuo',
                'super_lusso': 'uso amministratore',
                'SERVIZIO': '',  # gestione speciale
            }.get(classif, classif)
            if classif == 'SERVIZIO':
                new_name = f"Costo servizio \nft.n. {invoice_number}"
            else:
                new_name = f"Costo carburante ft.n. {invoice_number}\n{classe_label}"

            price = round(info_cls['totale'], 2)

            # Estraggo product/uom/analytic dalla POL per move_line
            _prod = pol.get('product_id')
            product_id = _prod[0] if isinstance(_prod, list) else _prod
            _uom = pol.get('product_uom')
            product_uom_id = _uom[0] if isinstance(_uom, list) else _uom
            _aa = pol.get('account_analytic_id')
            analytic_id = _aa[0] if isinstance(_aa, list) else _aa

            consumed_pol_info.append({
                'po_line_id': pol['id'],
                'oda_name': oda_name,
                'classif': classif,
                'old_price_unit': pol.get('price_unit') or 0,
                'old_name': pol.get('name') or '',
                'old_date_planned': pol.get('date_planned') or None,
                'old_taxes_id': list(pol.get('taxes_id') or []),
                'new_price': price,
                'new_name': new_name,
                'new_tax_id': tax_id,
                'account_id': account_id,
                'product_id': product_id,
                'product_uom_id': product_uom_id,
                'analytic_account_id': analytic_id,
                'n_righe_aggregate': info_cls['n_righe'],
                'n_carte_distinte': len(info_cls['carte']),
            })

            ml_vals = {
                'name': new_name,
                'account_id': account_id,
                'price_unit': price,
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

        # === Fase 4: build move_vals ===
        move_vals = {
            'move_type': 'in_invoice',
            'partner_id': partner_id,
            'invoice_date': invoice_date,
            'date': date_contabile,
            'l10n_it_vat_settlement_date': date_iva,
            'ref': invoice_number,
            'invoice_origin': oda_name,
            'journal_id': mapping_entry.get('journal_id', 2),
            'company_id': ECOTEL,
            'currency_id': currency_id,
            'invoice_line_ids': [(0, 0, ml) for ml in move_lines_vals],
        }

        # === DRY-RUN ===
        if self.dry_run:
            tot = sum(ml['price_unit'] for ml in move_lines_vals)
            consumed_str = '; '.join(
                f"POL {p['po_line_id']} {p['classif']} "
                f"EUR{p['old_price_unit']:.2f}->EUR{p['new_price']:.2f} "
                f"tax{p['new_tax_id']} acc{p['account_id']} "
                f"(n_righe={p['n_righe_aggregate']}, n_carte={p['n_carte_distinte']})"
                for p in consumed_pol_info)
            logger.info(
                f"[DRY_RUN] create_bozza_edenred_uta fornitore={nome_forn} "
                f"OdA={oda_name} classi={len(move_lines_vals)} "
                f"tot_imponibile={tot:.2f} consume-POL=[{consumed_str}]")
            if carte_non_mappate:
                logger.warning(
                    f"[DRY_RUN] {len(carte_non_mappate)} carte NON in mappa "
                    f"(fallback uso_promiscuo): {sorted(carte_non_mappate)[:10]}")
            if righe_senza_carta:
                logger.info(
                    f"[DRY_RUN] {righe_senza_carta} righe senza RiferimentoAmministrazione "
                    f"-> SERVIZIO")
            primary = consumed_pol_info[0]
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': p['classif']}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(success=True, action='create_draft_edenred_uta',
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
                logger.info(
                    f"Updated POL {p['po_line_id']} {p['classif']}: "
                    f"price={p['new_price']:.2f} acc={p['account_id']} tax={p['new_tax_id']}")

            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            if not move_id:
                raise RuntimeError("create move returned empty")
            logger.info(
                f"Created Edenred UTA move {move_id} OdA={oda_name} "
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

            # Marca fatturapa registered
            if analysis.attachment_id:
                try:
                    self.client._call('account.move', 'write', [move_id], {
                        'fatturapa_attachment_in_id': analysis.attachment_id,
                    })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito: {e}")

            if carte_non_mappate:
                logger.warning(
                    f"Edenred UTA: {len(carte_non_mappate)} carte NON in mappa "
                    f"(fallback uso_promiscuo): {sorted(carte_non_mappate)[:10]}. "
                    f"Aggiornare input/carte_uta.xlsx.")

            primary = consumed_pol_info[0]
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': p['classif']}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(success=True, action='create_draft_edenred_uta',
                               move_id=move_id, dry_run=False,
                               po_line_id=primary['po_line_id'],
                               old_price_unit=primary['old_price_unit'],
                               old_name=primary['old_name'],
                               old_date_planned=primary['old_date_planned'],
                               extra_po_lines=extras or None)
        except Exception as e:
            logger.exception("Errore create_bozza_edenred_uta")
            # Rollback POL
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
            return WriteResult(success=False, action='create_draft_edenred_uta',
                               error_message=str(e), dry_run=False)

    # ================================================================
    # === ENILIVE S.P.A. (IT11403240960) — carte carburante via PDF ===
    # ================================================================
    #
    # Differenza dal pattern Edenred UTA:
    #   - L'XML FatturaPA Enilive contiene SOLO righe aggregate per prodotto
    #     (BENZSP/DIESEL/ADBLUE/FEE), zero <RiferimentoAmministrazione>.
    #   - Il dettaglio carta-per-carta è SOLO nel PDF allegato. Lo parsa
    #     core.enilive_pdf_parser e ne ricava EniliveBreakdown.
    #   - L'OdA-ledger P03731 ha POL pre-pianificate quindicinali con
    #     keyword "AUTOMEZZI" (= POOL) e "AUTOVETTURE" (= uso_promiscuo).
    #     Per la fee servizio (FEE SICUREZZA E GEST) Acquisti NON crea POL:
    #     il writer la crea ad-hoc (added_po_line_ids per rollback).
    #
    # Routing fiscale Ecotel (identico a Edenred UTA):
    #   POOL          -> 410300 id=358   tax 11
    #   uso_promiscuo -> 410410 id=1125  tax 11
    #   super_lusso   -> 410400 id=359   tax 73
    #   SERVIZIO      -> 420190 id=1190  tax 11  (POL NUOVA, product 12202)
    _ENILIVE_ROUTING = {
        'POOL':          {'account_id': 358,  'tax_id': 11, 'pol_keyword': 'automezzi'},
        'uso_promiscuo': {'account_id': 1125, 'tax_id': 11, 'pol_keyword': 'autovetture'},
        'super_lusso':   {'account_id': 359,  'tax_id': 73, 'pol_keyword': 'amministratore'},
        'SERVIZIO':      {'account_id': 1190, 'tax_id': 11,
                          # POL NUOVA da creare: usa "Fornitura di Servizi"
                          # (id 12202, uom 68 PZ) che è il product già usato
                          # sulle altre POL P03731 (uniforme).
                          'new_pol_product_id': 12202,
                          'new_pol_product_uom_id': 68},
    }

    _RE_ENILIVE_POL_PERIOD = re.compile(
        r'dal\s+(\d{2}/\d{2}/\d{4})\s+al\s+(\d{2}/\d{2}/\d{4})',
        re.IGNORECASE,
    )

    @classmethod
    def _enilive_pol_period_includes(cls, pol_name: str, invoice_date_iso: str) -> bool:
        """True se il name della POL contiene un range 'dal X al Y' (DD/MM/YYYY)
        e invoice_date (YYYY-MM-DD) cade dentro quel range (incluso).

        Ritorna False se il name non ha un range (la POL non è quindicinale,
        es. 'Canone TRIM').
        """
        if not pol_name or not invoice_date_iso:
            return False
        m = cls._RE_ENILIVE_POL_PERIOD.search(pol_name)
        if not m:
            return False
        try:
            from datetime import date
            d_from = date(int(m.group(1)[6:10]), int(m.group(1)[3:5]),
                          int(m.group(1)[0:2]))
            d_to   = date(int(m.group(2)[6:10]), int(m.group(2)[3:5]),
                          int(m.group(2)[0:2]))
            y, mth, dd = invoice_date_iso.split('-')
            d_inv = date(int(y), int(mth), int(dd))
        except Exception:
            return False
        return d_from <= d_inv <= d_to

    def _match_enilive_pol_for_classe(self, libere: List[Dict], classe: str,
                                       invoice_date_iso: str,
                                       used_pol_ids: set) -> Optional[Dict]:
        """Match POL libera per Enilive in 3 livelli di priorità:
          1) name contiene keyword (AUTOMEZZI/AUTOVETTURE) AND
             range periodo include data fattura
          2) keyword match ma range non incluso (per fallback)
          3) keyword match a prescindere dal periodo (caso vecchie POL)

        Ritorna None se nessuna POL libera ha la keyword giusta.
        """
        routing = self._ENILIVE_ROUTING.get(classe)
        if not routing:
            return None
        keyword = routing.get('pol_keyword', '').lower()
        if not keyword:
            return None
        with_period_ok = []
        keyword_only = []
        for cand in libere:
            if cand['id'] in used_pol_ids:
                continue
            name = (cand.get('name') or '').lower()
            if keyword not in name:
                continue
            if self._enilive_pol_period_includes(cand.get('name') or '',
                                                   invoice_date_iso):
                with_period_ok.append(cand)
            else:
                keyword_only.append(cand)
        # Preferenza: periodo OK -> qualsiasi keyword match
        if with_period_ok:
            return with_period_ok[0]
        if keyword_only:
            return keyword_only[0]
        return None

    def create_bozza_enilive(self, analysis,
                              mapping_entry: Dict) -> WriteResult:
        """Crea bozza per fattura carte carburante Enilive S.p.A.
        (IT11403240960).

        Differenza chiave da create_bozza_edenred_uta: il dettaglio per carta
        sta nel PDF allegato, non nelle righe XML. Si usa
        `core.enilive_pdf_parser` per parsare il PDF e ottenere
        l'EniliveBreakdown (carte + fee_sicurezza + classificazione).

        Steps:
          1) Estrai PDF allegato da analysis.raw_xml
          2) Parsa breakdown carte (con classificazione)
          3) Aggrega per classe POOL/uso_promiscuo/super_lusso
          4) Per ogni classe match POL libera su P03731 per keyword+periodo:
             - riscrivi POL (name, price, tax, qty_received=1)
             - crea move_line collegata
          5) Se c'è FEE SICUREZZA, crea NUOVA POL (account 420190, product
             "Fornitura di Servizi"=12202) e relativa move_line

        Returns WriteResult con po_line_id primaria + extra_po_lines per le
        riassegnazioni + added_po_line_ids per la nuova POL SERVIZIO.
        """
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc != 'TD01':
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message=f"Tipo {tipo_doc} non supportato (atteso TD01)",
                               dry_run=self.dry_run)

        vat = (analysis.xml_data.cedente_partita_iva or '').strip().upper()
        if vat != 'IT11403240960':
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message=f"P.IVA {vat} non Enilive",
                               dry_run=self.dry_run)

        # === Fase 0: parsing PDF allegato ===
        from core.enilive_pdf_parser import (
            extract_attached_pdf_from_xml, parse_enilive_pdf,
        )
        pdf_bytes = extract_attached_pdf_from_xml(analysis.raw_xml or '')
        if not pdf_bytes:
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message="PDF allegato non trovato nella fattura XML",
                               dry_run=self.dry_run)
        try:
            breakdown = parse_enilive_pdf(pdf_bytes)
        except Exception as e:
            logger.exception("Errore parsing PDF Enilive")
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message=f"Parsing PDF Enilive fallito: {e}",
                               dry_run=self.dry_run)
        if not breakdown.carte:
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message="PDF Enilive: nessuna carta estratta",
                               dry_run=self.dry_run)

        # Aggrego per classe + verifica carte non in mappa
        agg_classi = breakdown.aggregate_by_classe()
        carte_non_mappate = list(breakdown.carte_non_in_mappa)
        # Per ora le carte non in mappa vengono dirottate su uso_promiscuo
        # (fallback conservativo come Edenred UTA). Se ce ne sono, viene loggato
        # un warning.
        if '_NON_IN_MAPPA' in agg_classi:
            unmapped = agg_classi.pop('_NON_IN_MAPPA')
            fallback = agg_classi.setdefault('uso_promiscuo', {
                'imponibile': 0.0, 'iva': 0.0, 'totale': 0.0, 'carte': [],
            })
            fallback['imponibile'] = round(fallback['imponibile'] + unmapped['imponibile'], 2)
            fallback['iva']        = round(fallback['iva']        + unmapped['iva'], 2)
            fallback['totale']     = round(fallback['totale']     + unmapped['totale'], 2)
            fallback['carte'].extend(unmapped['carte'])

        nome_forn = mapping_entry.get('nome', 'Enilive S.p.A.')
        oda_name = mapping_entry.get('oda_fisso', 'P03731')

        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        date_contabile = self._data_contabile(analysis, invoice_date)
        date_iva = self._end_of_month(invoice_date)

        # === Fase 1: trovo OdA + POL libere ===
        ECOTEL = 1
        pos = self.client._call('purchase.order', 'search_read',
            [('name', '=', oda_name), ('company_id', '=', ECOTEL)],
            fields=['id', 'name', 'state', 'partner_id', 'currency_id'],
            limit=1)
        if not pos:
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message=f"OdA {oda_name} non trovato su Ecotel",
                               dry_run=self.dry_run)
        po = pos[0]
        po_id = po['id']
        partner_id = (po['partner_id'][0]
                       if isinstance(po['partner_id'], list) else None)
        currency_id = (po['currency_id'][0]
                        if isinstance(po['currency_id'], list) else None)
        if not partner_id:
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message=f"OdA {oda_name} senza partner",
                               dry_run=self.dry_run)

        libere = self._find_libere_purchase_order_lines(po_id, 'standard_qty_inv_rec')

        # POOL/uso_promiscuo/super_lusso vanno su POL pre-pianificate (cardano).
        # SERVIZIO va su una NUOVA POL creata ad hoc.
        classi_cardanti = [c for c in ('POOL', 'uso_promiscuo', 'super_lusso')
                            if c in agg_classi]
        n_pol_libere_richieste = len(classi_cardanti)
        if len(libere) < n_pol_libere_richieste:
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message=(
                                   f"POL libere insufficienti su {oda_name}: "
                                   f"{len(libere)} disponibili, "
                                   f"{n_pol_libere_richieste} servono "
                                   f"(1 per classe cardante: {classi_cardanti})"),
                               dry_run=self.dry_run)

        # === Fase 2: match POL per classe ===
        consumed_pol_info = []
        move_lines_vals = []
        used_pol_ids: set = set()

        # Ordine fisso per stabilità output (come Edenred UTA)
        ORDER_CARDANTI = ['POOL', 'uso_promiscuo', 'super_lusso']
        classi_ordinate = [c for c in ORDER_CARDANTI if c in agg_classi]

        for classif in classi_ordinate:
            info_cls = agg_classi[classif]
            routing = self._ENILIVE_ROUTING.get(classif)
            if not routing:
                return WriteResult(success=False, action='create_draft_enilive',
                                   error_message=f"Routing non definito per {classif!r}",
                                   dry_run=self.dry_run)
            pol = self._match_enilive_pol_for_classe(libere, classif,
                                                      invoice_date, used_pol_ids)
            if pol is None:
                return WriteResult(success=False, action='create_draft_enilive',
                                   error_message=(
                                       f"Nessuna POL libera per classe {classif!r} "
                                       f"(keyword '{routing['pol_keyword']}') su {oda_name}. "
                                       f"Servono nuove POL da Acquisti."),
                                   dry_run=self.dry_run)
            used_pol_ids.add(pol['id'])

            classe_label_human = {
                'POOL': 'AUTOMEZZI',
                'uso_promiscuo': 'AUTOVETTURE',
                'super_lusso': 'AMMINISTRATORE',
            }.get(classif, classif)
            new_name = f"Costo carburante ft.n.{invoice_number} - {classe_label_human}"
            price = round(info_cls['imponibile'], 2)

            _prod = pol.get('product_id')
            product_id = _prod[0] if isinstance(_prod, list) else _prod
            _uom = pol.get('product_uom')
            product_uom_id = _uom[0] if isinstance(_uom, list) else _uom
            _aa = pol.get('account_analytic_id')
            analytic_id = _aa[0] if isinstance(_aa, list) else _aa

            consumed_pol_info.append({
                'po_line_id': pol['id'],
                'oda_name': oda_name,
                'classif': classif,
                'old_price_unit': pol.get('price_unit') or 0,
                'old_name': pol.get('name') or '',
                'old_date_planned': pol.get('date_planned') or None,
                'old_taxes_id': list(pol.get('taxes_id') or []),
                'new_price': price,
                'new_name': new_name,
                'new_tax_id': routing['tax_id'],
                'account_id': routing['account_id'],
                'product_id': product_id,
                'product_uom_id': product_uom_id,
                'analytic_account_id': analytic_id,
                'n_carte_distinte': len(info_cls['carte']),
            })

            ml_vals = {
                'name': new_name,
                'account_id': routing['account_id'],
                'price_unit': price,
                'quantity': 1,
                'tax_ids': [(6, 0, [routing['tax_id']])],
                'purchase_line_id': pol['id'],
            }
            if product_id:
                ml_vals['product_id'] = product_id
            if product_uom_id:
                ml_vals['product_uom_id'] = product_uom_id
            if analytic_id:
                ml_vals['analytic_account_id'] = analytic_id
            move_lines_vals.append(ml_vals)

        # === Fase 3: NUOVA POL SERVIZIO (se c'è FEE SICUREZZA) ===
        new_servizio_pol_id = None
        new_servizio_pol_info = None
        if breakdown.fee_sicurezza:
            srv = self._ENILIVE_ROUTING['SERVIZIO']
            srv_imponibile = round(breakdown.fee_sicurezza.imponibile, 2)
            srv_pol_name = f"Fee Sicurezza e Gestione ft.n.{invoice_number}"
            # account_analytic_id ereditato dalle POL cardanti (tutte le POL di
            # P03731 condividono lo stesso analytic, es. 4225 S03869).
            srv_analytic_id = None
            if consumed_pol_info:
                srv_analytic_id = consumed_pol_info[0].get('analytic_account_id')
            if not srv_analytic_id:
                # Fallback: pesco l'analytic da una POL qualsiasi del PO
                # (caso degenere: fattura con sola FEE, zero carte).
                any_pols = self.client._call('purchase.order.line', 'search_read',
                    [('order_id', '=', po_id)],
                    fields=['account_analytic_id'], limit=1)
                if any_pols:
                    _aa = any_pols[0].get('account_analytic_id')
                    srv_analytic_id = _aa[0] if isinstance(_aa, list) else _aa
            new_servizio_pol_info = {
                'oda_name': oda_name,
                'order_id': po_id,
                'name': srv_pol_name,
                'product_id': srv['new_pol_product_id'],
                'product_uom': srv['new_pol_product_uom_id'],
                'product_qty': 1,
                'price_unit': srv_imponibile,
                'taxes_id': [(6, 0, [srv['tax_id']])],
                'date_planned': invoice_date,
                'analytic_account_id': srv_analytic_id,
            }

        # === Fase 4: build move_vals ===
        # Aggiungo la move_line SERVIZIO solo se la POL verrà creata. Per il
        # DRY-RUN simulo l'id come None; in produzione il purchase_line_id
        # verrà settato dopo la create della POL.
        move_vals_base = {
            'move_type': 'in_invoice',
            'partner_id': partner_id,
            'invoice_date': invoice_date,
            'date': date_contabile,
            'l10n_it_vat_settlement_date': date_iva,
            'ref': invoice_number,
            'invoice_origin': oda_name,
            'journal_id': mapping_entry.get('journal_id', 2),
            'company_id': ECOTEL,
            'currency_id': currency_id,
        }

        # === DRY-RUN ===
        if self.dry_run:
            tot_lines = sum(ml['price_unit'] for ml in move_lines_vals)
            if breakdown.fee_sicurezza:
                tot_lines += breakdown.fee_sicurezza.imponibile
            consumed_str = '; '.join(
                f"POL {p['po_line_id']} {p['classif']} "
                f"EUR{p['old_price_unit']:.2f}->EUR{p['new_price']:.2f} "
                f"acc{p['account_id']} tax{p['new_tax_id']} (carte={p['n_carte_distinte']})"
                for p in consumed_pol_info)
            extra_msg = (f' + new POL SERVIZIO EUR{breakdown.fee_sicurezza.imponibile:.2f}'
                          if breakdown.fee_sicurezza else '')
            logger.info(
                f"[DRY_RUN] create_bozza_enilive fornitore={nome_forn} "
                f"OdA={oda_name} classi={len(move_lines_vals)} "
                f"tot_imponibile={tot_lines:.2f} consume-POL=[{consumed_str}]{extra_msg}")
            if carte_non_mappate:
                logger.warning(
                    f"[DRY_RUN] {len(carte_non_mappate)} carte NON in mappa "
                    f"(fallback uso_promiscuo): {carte_non_mappate}")
            primary = consumed_pol_info[0] if consumed_pol_info else None
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': p['classif']}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(
                success=True, action='create_draft_enilive',
                move_id=None, dry_run=True,
                po_line_id=primary['po_line_id'] if primary else None,
                old_price_unit=primary['old_price_unit'] if primary else None,
                old_name=primary['old_name'] if primary else None,
                old_date_planned=primary['old_date_planned'] if primary else None,
                extra_po_lines=extras or None,
                # added_po_line_ids: lista di POL CREATE — in dry-run è None
                # perché la POL SERVIZIO non viene scritta.
            )

        # === SCRITTURA REALE ===
        try:
            # 1) Riscrivo POL cardanti (POOL / uso_promiscuo / super_lusso)
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
                logger.info(
                    f"Enilive: updated POL {p['po_line_id']} {p['classif']} "
                    f"price={p['new_price']:.2f} acc={p['account_id']} tax={p['new_tax_id']}")

            # 2) Creo NUOVA POL SERVIZIO (se c'è FEE SICUREZZA)
            if new_servizio_pol_info:
                srv = self._ENILIVE_ROUTING['SERVIZIO']
                new_pol_vals = {
                    'order_id': po_id,
                    'name': new_servizio_pol_info['name'],
                    'product_id': new_servizio_pol_info['product_id'],
                    'product_uom': new_servizio_pol_info['product_uom'],
                    'product_qty': new_servizio_pol_info['product_qty'],
                    'price_unit': new_servizio_pol_info['price_unit'],
                    'taxes_id': new_servizio_pol_info['taxes_id'],
                    'date_planned': new_servizio_pol_info['date_planned'],
                    # qty_received per consumare subito
                    'qty_received': 1,
                    'qty_received_manual': 1,
                }
                if new_servizio_pol_info.get('analytic_account_id'):
                    new_pol_vals['account_analytic_id'] = (
                        new_servizio_pol_info['analytic_account_id'])
                new_pol_id = self.client._call('purchase.order.line', 'create',
                                                 new_pol_vals)
                if isinstance(new_pol_id, list):
                    new_pol_id = new_pol_id[0] if new_pol_id else None
                if not new_pol_id:
                    raise RuntimeError("create POL SERVIZIO returned empty")
                new_servizio_pol_id = new_pol_id
                logger.info(
                    f"Enilive: created NEW POL {new_pol_id} SERVIZIO "
                    f"price={new_servizio_pol_info['price_unit']:.2f} "
                    f"on OdA={oda_name}")

                # Aggiungo move_line collegata alla nuova POL
                ml_vals = {
                    'name': new_servizio_pol_info['name'],
                    'account_id': srv['account_id'],
                    'price_unit': new_servizio_pol_info['price_unit'],
                    'quantity': 1,
                    'tax_ids': new_servizio_pol_info['taxes_id'],
                    'purchase_line_id': new_pol_id,
                    'product_id': srv['new_pol_product_id'],
                    'product_uom_id': srv['new_pol_product_uom_id'],
                }
                if new_servizio_pol_info.get('analytic_account_id'):
                    ml_vals['analytic_account_id'] = (
                        new_servizio_pol_info['analytic_account_id'])
                move_lines_vals.append(ml_vals)

            # 3) Creo il move
            move_vals = dict(move_vals_base)
            move_vals['invoice_line_ids'] = [(0, 0, ml) for ml in move_lines_vals]
            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            if not move_id:
                raise RuntimeError("create move returned empty")
            logger.info(
                f"Created Enilive move {move_id} OdA={oda_name} "
                f"consume-POL={[p['po_line_id'] for p in consumed_pol_info]} "
                f"new-POL-servizio={new_servizio_pol_id}")

            # 4) Allego XML
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

            # 5) Marca fatturapa registered
            if analysis.attachment_id:
                try:
                    self.client._call('account.move', 'write', [move_id], {
                        'fatturapa_attachment_in_id': analysis.attachment_id,
                    })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito: {e}")

            if carte_non_mappate:
                logger.warning(
                    f"Enilive: {len(carte_non_mappate)} carte NON in mappa "
                    f"(fallback uso_promiscuo): {carte_non_mappate}. "
                    f"Aggiornare input/carte_enilive.xlsx.")

            primary = consumed_pol_info[0]
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': p['classif']}
                for p in consumed_pol_info[1:]
            ]
            added = [new_servizio_pol_id] if new_servizio_pol_id else None
            return WriteResult(success=True, action='create_draft_enilive',
                               move_id=move_id, dry_run=False,
                               po_line_id=primary['po_line_id'],
                               old_price_unit=primary['old_price_unit'],
                               old_name=primary['old_name'],
                               old_date_planned=primary['old_date_planned'],
                               extra_po_lines=extras or None,
                               added_po_line_ids=added)

        except Exception as e:
            logger.exception("Errore create_bozza_enilive")
            # Rollback POL riassegnate
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
            # Rollback POL SERVIZIO creata
            if new_servizio_pol_id:
                try:
                    self.client._call('purchase.order.line', 'unlink',
                                       [new_servizio_pol_id])
                except Exception:
                    logger.warning(f"Unlink new POL {new_servizio_pol_id} fallito")
            return WriteResult(success=False, action='create_draft_enilive',
                               error_message=str(e), dry_run=False)

    # ====================================================================
    # === LEASYS ITALIA (IT06714021000) — POL aggregate + ad-hoc extra ===
    # ====================================================================
    #
    # Schema deciso 13/05/2026 (vedi memoria project_session_2026_05_13_leasys_refactor):
    #
    # A) CANONI (CANONE LOCAZIONE / CANONE SERVIZIO):
    #    - Aggregati per (cls × voce) → max 4 bucket
    #    - Acquisti pre-pianifica 4 POL/mese su P03021 con name generico tipo
    #      "MAGGIO 2026 canone locazione POOL" (synonimi: automezzi/furgoni)
    #      "MAGGIO 2026 canone servizio USO PROMISCUO" (synonimi: uso promiscuo/autovetture)
    #    - Per multi-fattura nello stesso mese Acquisti ripete i 4 nomi N volte
    #    - Writer FIFO per id ascendente, riassegna POL: price=tot_bucket,
    #      name=<vecchio name> ft.<numero fattura>, qty_received=1, tax 6,
    #      account 430210/220/230/240, analytic ereditato
    #
    # B) VOCI EXTRA (tutto il resto: bolli/riaddebiti/penali/manutenzioni):
    #    - Aggregate per (voce_extra × cls)
    #    - Writer CREA POL ad-hoc completa (pattern Enilive FEE SICUREZZA)
    #    - Routing:
    #        tassa (IVA 0%) POOL          → 490300 (id 161), tax 47
    #        tassa (IVA 0%) uso_promiscuo → 490410 (id 1129), tax 47
    #        altro (IVA 22%) POOL         → 430230 (id 400), tax 6
    #        altro (IVA 22%) uso_promiscuo→ 430240 (id 401), tax 6
    #    - POL ad-hoc tracciate in added_po_line_ids per rollback

    _LEASYS_CANONI_ROUTING = {
        # (voce, cls) -> account_id move + tax_id
        ('locazione', 'POOL'):          {'account_id':  398, 'tax_id': 6},
        ('servizi',   'POOL'):          {'account_id':  400, 'tax_id': 6},
        ('locazione', 'uso_promiscuo'): {'account_id':  399, 'tax_id': 6},
        ('servizi',   'uso_promiscuo'): {'account_id':  401, 'tax_id': 6},
        ('locazione', 'super_lusso'):   {'account_id': 1119, 'tax_id': 73},
        ('servizi',   'super_lusso'):   {'account_id': 1120, 'tax_id': 73},
    }

    _LEASYS_EXTRA_ROUTING = {
        # (voce_extra, cls) -> account_id, tax_id, label (per name POL)
        ('tassa', 'POOL'):          {'account_id':  161, 'tax_id': 47,
                                      'label': 'Riaddebito tassa POOL'},
        ('tassa', 'uso_promiscuo'): {'account_id': 1129, 'tax_id': 47,
                                      'label': 'Riaddebito tassa USO PROMISCUO'},
        ('altro', 'POOL'):          {'account_id':  400, 'tax_id':  6,
                                      'label': 'Altri servizi POOL'},
        ('altro', 'uso_promiscuo'): {'account_id':  401, 'tax_id':  6,
                                      'label': 'Altri servizi USO PROMISCUO'},
    }

    # Product Odoo "Fornitura di Servizi" (id 12202, uom 68 PZ) — coerente
    # con le altre POL P03021 esistenti
    _LEASYS_NEW_POL_PRODUCT_ID = 12202
    _LEASYS_NEW_POL_PRODUCT_UOM_ID = 68

    _RE_LEASYS_TARGA = re.compile(r'\b([A-Z]{2}\d{3}[A-Z]{2})\b')

    _MESI_IT = ('', 'GENNAIO', 'FEBBRAIO', 'MARZO', 'APRILE', 'MAGGIO',
                'GIUGNO', 'LUGLIO', 'AGOSTO', 'SETTEMBRE', 'OTTOBRE',
                'NOVEMBRE', 'DICEMBRE')

    @classmethod
    def _mese_competenza_leasys(cls, righe, invoice_date_iso: str) -> Optional[str]:
        """Deduce il nome del mese di competenza per una fattura Leasys.

        Priorità:
          1) DataInizioPeriodo della prima riga XML (FatturaPA standard).
             Es: 2026-06-01 -> 'GIUGNO'
          2) Fallback Leasys: mese fattura + 1 (canoni emessi al mese precedente)
             Es: invoice_date 2026-05-08 -> mese 5+1=6 -> 'GIUGNO'

        Ritorna None se impossibile dedurre (es. invoice_date malformato).
        """
        # 1) DataInizioPeriodo dalla prima riga utile
        for r in righe or []:
            dip = getattr(r, 'data_inizio_periodo', '') or ''
            if dip and len(dip) >= 7:
                try:
                    mese = int(dip[5:7])
                    if 1 <= mese <= 12:
                        return cls._MESI_IT[mese]
                except (ValueError, IndexError):
                    continue
        # 2) Fallback: mese fattura + 1
        try:
            y, m, _ = invoice_date_iso.split('-')
            mese = (int(m) % 12) + 1
            return cls._MESI_IT[mese]
        except Exception:
            return None

    @classmethod
    def _classify_voce_leasys(cls, descrizione: str, aliquota_iva: float) -> str:
        """Classifica la voce di una riga XML Leasys.

        - 'locazione' se desc contiene 'CANONE LOCAZIONE'
        - 'servizi'   se desc contiene 'CANONE SERVIZIO' (singolare o plurale)
        - 'tassa'     se aliquota_iva == 0 (riaddebito bollo/tasse)
        - 'altro'     fallback (penali/manutenzioni/varie IVA 22%)
        """
        desc_u = (descrizione or '').upper()
        if 'CANONE LOCAZIONE' in desc_u:
            return 'locazione'
        if 'CANONE SERVIZIO' in desc_u or 'CANONE SERVIZI' in desc_u:
            return 'servizi'
        if (aliquota_iva or 0) == 0:
            return 'tassa'
        return 'altro'

    @classmethod
    def _classify_cls_leasys(cls, targa: str, contratto: str = '') -> str:
        """Lookup cls fiscale veicolo da targa/contratto via PARCO_AUTO.

        Il PARCO_AUTO autogen usa la chiave 'classificazione' (senza suffisso
        _fiscale). Manteniamo entrambe per compatibilità con eventuali estensioni
        future.

        Fallback conservativo: 'uso_promiscuo' (deducib. 70%) se non in mappa.
        """
        try:
            from config.parco_auto_mapping import (
                PARCO_BY_TARGA, PARCO_BY_CONTRATTO,
            )
        except ImportError:
            return 'uso_promiscuo'
        def _read_cls(info):
            return (info.get('classificazione')
                    or info.get('classificazione_fiscale'))
        if targa:
            info = PARCO_BY_TARGA.get(targa.upper())
            if info:
                v = _read_cls(info)
                if v:
                    return v
        if contratto:
            info = PARCO_BY_CONTRATTO.get(str(contratto).strip())
            if info:
                v = _read_cls(info)
                if v:
                    return v
        return 'uso_promiscuo'

    def _match_leasys_canone_pol(self, libere: List[Dict], voce: str, cls: str,
                                   used_ids: set,
                                   mese_competenza: Optional[str] = None) -> Optional[Dict]:
        """Cerca una POL libera "canone" matchante (voce + cls + mese), FIFO id ASC.

        Match in 3 livelli di priorità decrescente:
          1) Esatto: voce + cls + mese_competenza nel name (es. 'GIUGNO 2026 canone locazione POOL')
          2) voce + cls (qualsiasi mese) — fallback se mese specifico esaurito
          3) Solo voce (cls dedotta dal name, qualsiasi mese)

        Le `libere` sono già ordinate per (price_unit, id) da
        _find_libere_purchase_order_lines, ma re-sortiamo per id ASC qui per il
        pattern multi-fattura con name uguali (coppie A/B).
        """
        libere_sorted = sorted([l for l in libere if l['id'] not in used_ids],
                                key=lambda l: l['id'])
        # Livello 1: voce + cls + mese
        if mese_competenza:
            for cand in libere_sorted:
                name = cand.get('name') or ''
                if mese_competenza not in name.upper():
                    continue
                if not self._pol_name_matches_voce(cand, voce):
                    continue
                if self._classify_cls_from_pol_name(name) == cls:
                    return cand
        # Livello 2: voce + cls (qualsiasi mese)
        for cand in libere_sorted:
            name = cand.get('name') or ''
            if not self._pol_name_matches_voce(cand, voce):
                continue
            if self._classify_cls_from_pol_name(name) == cls:
                return cand
        # Livello 3: solo voce (cls inferita dal name)
        for cand in libere_sorted:
            if self._pol_name_matches_voce(cand, voce):
                return cand
        return None

    def _create_bozza_leasys_aggregated(self, analysis,
                                          mapping_entry: Dict) -> WriteResult:
        """Crea bozza Leasys con pattern aggregato (4 POL canoni + N POL extra).

        Steps:
          1) Aggrega righe XML per (cls × voce_canone) e (cls × voce_extra)
          2) Per ogni bucket canone: match POL pre-pianificata su P03021, riassegna
          3) Per ogni bucket extra: crea POL ad-hoc (account/tax/analytic/product)
          4) Crea account.move con N move_line (una per bucket non vuoto)
          5) DRY-RUN o scrittura reale + rollback su errore

        action='create_draft_automezzi' (compatibile con audit DB esistente).
        """
        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)
        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01', 'TD04'):
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message=f"Tipo {tipo_doc} non supportato (atteso TD01/TD04)",
                               dry_run=self.dry_run)
        is_nota_credito = (tipo_doc == 'TD04')

        righe = analysis.xml_data.righe or []
        if not righe:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message="Nessuna riga in XML",
                               dry_run=self.dry_run)

        nome_forn = mapping_entry.get('nome', 'Leasys Italia S.p.A.')
        oda_name = mapping_entry.get('oda_fisso', 'P03021')

        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        date_contabile = self._data_contabile(analysis, invoice_date)
        date_iva = self._end_of_month(invoice_date)

        # Mese competenza (da DataInizioPeriodo della prima riga o fallback
        # mese fattura +1) -> filtro POL solo del mese giusto, evita di
        # consumare per errore POL di mesi successivi durante FIFO.
        mese_competenza = self._mese_competenza_leasys(righe, invoice_date)

        # === Fase 1: aggregazione righe per (voce, cls) ===
        # Buckets canoni: voce ∈ {locazione, servizi} → match POL pre-pianificate
        # Buckets extra: voce ∈ {tassa, altro} → POL ad-hoc create dal writer
        bucket_canoni = {}  # (voce, cls) -> {imp, iva, n_righe, targhe}
        bucket_extra  = {}  # (voce, cls) -> {imp, iva, n_righe, targhe, desc_sample}
        targhe_unmapped = set()

        for r in righe:
            desc = r.descrizione or ''
            voce = self._classify_voce_leasys(desc, r.aliquota_iva or 0)
            m = self._RE_LEASYS_TARGA.search(desc.upper())
            targa = m.group(1) if m else None
            cls = self._classify_cls_leasys(targa)
            if targa and cls == 'uso_promiscuo':
                # Verifica se era davvero default (per warning, no impatto cls)
                try:
                    from config.parco_auto_mapping import PARCO_BY_TARGA
                    if targa not in PARCO_BY_TARGA:
                        targhe_unmapped.add(targa)
                except ImportError:
                    pass

            prezzo = float(r.prezzo_totale or 0)
            iva_amt = prezzo * (r.aliquota_iva or 0) / 100
            key = (voce, cls)
            target_bucket = bucket_canoni if voce in ('locazione', 'servizi') else bucket_extra
            if key not in target_bucket:
                target_bucket[key] = {
                    'imp': 0.0, 'iva': 0.0, 'n_righe': 0,
                    'targhe': set(), 'desc_sample': desc[:80],
                }
            target_bucket[key]['imp'] += prezzo
            target_bucket[key]['iva'] += iva_amt
            target_bucket[key]['n_righe'] += 1
            if targa:
                target_bucket[key]['targhe'].add(targa)

        if not bucket_canoni and not bucket_extra:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message="Nessuna riga aggregabile",
                               dry_run=self.dry_run)

        # === Fase 2: trovo OdA P03021 + POL libere ===
        ECOTEL = 1
        pos = self.client._call('purchase.order', 'search_read',
            [('name', '=', oda_name), ('company_id', '=', ECOTEL)],
            fields=['id', 'name', 'state', 'partner_id', 'currency_id'],
            limit=1)
        if not pos:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message=f"OdA {oda_name} non trovato su Ecotel",
                               dry_run=self.dry_run)
        po = pos[0]
        po_id = po['id']
        partner_id = (po['partner_id'][0]
                       if isinstance(po['partner_id'], list) else None)
        currency_id = (po['currency_id'][0]
                        if isinstance(po['currency_id'], list) else None)
        if not partner_id:
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message=f"OdA {oda_name} senza partner",
                               dry_run=self.dry_run)

        libere = self._find_libere_purchase_order_lines(po_id, 'standard_qty_inv_rec')

        # Recupero analytic_account_id ereditato da una POL del PO (4225 S03869)
        srv_analytic_id = None
        if libere:
            _aa = libere[0].get('account_analytic_id')
            srv_analytic_id = _aa[0] if isinstance(_aa, list) else _aa
        if not srv_analytic_id:
            any_pols = self.client._call('purchase.order.line', 'search_read',
                [('order_id', '=', po_id)],
                fields=['account_analytic_id'], limit=1)
            if any_pols:
                _aa = any_pols[0].get('account_analytic_id')
                srv_analytic_id = _aa[0] if isinstance(_aa, list) else _aa

        # === Fase 3: match POL pre-pianificate per bucket canoni ===
        consumed_pol_info = []  # POL riassegnate
        move_lines_vals = []
        used_pol_ids: set = set()

        # Ordine fisso per stabilità output
        ORDER_VOCI = ['locazione', 'servizi']
        ORDER_CLS  = ['POOL', 'uso_promiscuo', 'super_lusso']
        for cls in ORDER_CLS:
            for voce in ORDER_VOCI:
                if (voce, cls) not in bucket_canoni:
                    continue
                bk = bucket_canoni[(voce, cls)]
                pol = self._match_leasys_canone_pol(libere, voce, cls,
                                                      used_pol_ids,
                                                      mese_competenza=mese_competenza)
                if pol is None:
                    mese_hint = (f" (mese competenza {mese_competenza})"
                                  if mese_competenza else "")
                    return WriteResult(success=False, action='create_draft_automezzi',
                                       error_message=(
                                           f"Nessuna POL canone libera su {oda_name} "
                                           f"per ({voce}, {cls}){mese_hint}. Acquisti deve pre-pianificare "
                                           f"una POL '<{mese_competenza or 'MESE'}> canone {voce} "
                                           f"{cls.upper().replace('_',' ')}'."),
                                       dry_run=self.dry_run)
                used_pol_ids.add(pol['id'])

                routing = self._LEASYS_CANONI_ROUTING.get((voce, cls))
                if not routing:
                    return WriteResult(success=False, action='create_draft_automezzi',
                                       error_message=f"Routing canone non definito per ({voce}, {cls})",
                                       dry_run=self.dry_run)

                # Name post-consumo: append ft.NUMERO al name esistente
                old_name = pol.get('name') or ''
                new_name = old_name
                if invoice_number and f'ft.{invoice_number}' not in new_name:
                    new_name = f"{old_name} ft.{invoice_number}"

                # Segno per NC TD04: nel writer Leasys i canoni hanno sempre price +,
                # eccetto NC dove price_unit va negativo (convenzione Ecotel TD04)
                price = round(bk['imp'], 2)
                if is_nota_credito:
                    price = -abs(price)

                _prod = pol.get('product_id')
                product_id = _prod[0] if isinstance(_prod, list) else _prod
                _uom = pol.get('product_uom')
                product_uom_id = _uom[0] if isinstance(_uom, list) else _uom
                _aa = pol.get('account_analytic_id')
                analytic_id = (_aa[0] if isinstance(_aa, list) else _aa) or srv_analytic_id

                consumed_pol_info.append({
                    'po_line_id': pol['id'],
                    'oda_name': oda_name,
                    'voce': voce, 'cls': cls,
                    'old_price_unit': pol.get('price_unit') or 0,
                    'old_name': old_name,
                    'old_date_planned': pol.get('date_planned') or None,
                    'old_taxes_id': list(pol.get('taxes_id') or []),
                    'new_price': price,
                    'new_name': new_name,
                    'new_tax_id': routing['tax_id'],
                    'account_id': routing['account_id'],
                    'product_id': product_id,
                    'product_uom_id': product_uom_id,
                    'analytic_account_id': analytic_id,
                    'n_righe_aggregate': bk['n_righe'],
                    'n_targhe_distinte': len(bk['targhe']),
                })

                qty_signed = -1 if is_nota_credito else 1
                ml_vals = {
                    'name': new_name,
                    'account_id': routing['account_id'],
                    'price_unit': price,
                    'quantity': qty_signed,
                    'tax_ids': [(6, 0, [routing['tax_id']])],
                    'purchase_line_id': pol['id'],
                }
                if product_id:
                    ml_vals['product_id'] = product_id
                if product_uom_id:
                    ml_vals['product_uom_id'] = product_uom_id
                if analytic_id:
                    ml_vals['analytic_account_id'] = analytic_id
                move_lines_vals.append(ml_vals)

        # === Fase 4: POL ad-hoc per bucket extra ===
        new_pol_info_list = []   # [{order_id, name, ...}, ...] per scrittura
        ORDER_EXTRA_VOCI = ['tassa', 'altro']
        for cls in ORDER_CLS:
            for voce in ORDER_EXTRA_VOCI:
                if (voce, cls) not in bucket_extra:
                    continue
                bk = bucket_extra[(voce, cls)]
                routing = self._LEASYS_EXTRA_ROUTING.get((voce, cls))
                if not routing:
                    return WriteResult(success=False, action='create_draft_automezzi',
                                       error_message=f"Routing extra non definito per ({voce}, {cls})",
                                       dry_run=self.dry_run)
                imp = round(bk['imp'], 2)
                if is_nota_credito:
                    imp = -abs(imp)
                pol_name = f"{routing['label']} ft.{invoice_number}"
                new_pol_info_list.append({
                    'order_id': po_id,
                    'name': pol_name,
                    'product_id': self._LEASYS_NEW_POL_PRODUCT_ID,
                    'product_uom': self._LEASYS_NEW_POL_PRODUCT_UOM_ID,
                    'product_qty': 1,
                    'price_unit': imp,
                    'taxes_id': [(6, 0, [routing['tax_id']])],
                    'date_planned': invoice_date,
                    'account_analytic_id': srv_analytic_id,
                    # info per move_line costruita dopo create POL
                    '_voce': voce, '_cls': cls, '_imp': imp,
                    '_account_id': routing['account_id'],
                    '_tax_id': routing['tax_id'],
                })

        # === Fase 5: build move_vals_base ===
        move_type = 'in_refund' if is_nota_credito else 'in_invoice'
        move_vals_base = {
            'move_type': move_type,
            'partner_id': partner_id,
            'invoice_date': invoice_date,
            'date': date_contabile,
            'l10n_it_vat_settlement_date': date_iva,
            'ref': invoice_number,
            'invoice_origin': oda_name,
            'journal_id': mapping_entry.get('journal_id', 2),
            'company_id': ECOTEL,
            'currency_id': currency_id,
        }

        # === DRY-RUN ===
        if self.dry_run:
            tot_canoni = sum(p['new_price'] for p in consumed_pol_info)
            tot_extra = sum(p['_imp'] for p in new_pol_info_list)
            consumed_str = '; '.join(
                f"POL {p['po_line_id']} ({p['voce']}/{p['cls']}) "
                f"EUR{p['old_price_unit']:.2f}->EUR{p['new_price']:.2f} acc{p['account_id']}"
                for p in consumed_pol_info)
            extras_str = '; '.join(
                f"NEW POL ({p['_voce']}/{p['_cls']}) EUR{p['_imp']:.2f} "
                f"acc{p['_account_id']} tax{p['_tax_id']}"
                for p in new_pol_info_list)
            logger.info(
                f"[DRY_RUN] _create_bozza_leasys_aggregated fornitore={nome_forn} "
                f"OdA={oda_name} doc={invoice_number} type={tipo_doc} "
                f"canoni_buckets={len(consumed_pol_info)} (tot={tot_canoni:.2f}) "
                f"extra_buckets={len(new_pol_info_list)} (tot={tot_extra:.2f}) "
                f"consume-POL=[{consumed_str}] extras=[{extras_str}]")
            if targhe_unmapped:
                logger.warning(
                    f"[DRY_RUN] Leasys: {len(targhe_unmapped)} targhe NON in PARCO "
                    f"(fallback uso_promiscuo): {sorted(targhe_unmapped)[:10]}")
            primary = consumed_pol_info[0] if consumed_pol_info else None
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': f"{p['voce']}/{p['cls']}"}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(
                success=True, action='create_draft_automezzi',
                move_id=None, dry_run=True,
                po_line_id=primary['po_line_id'] if primary else None,
                old_price_unit=primary['old_price_unit'] if primary else None,
                old_name=primary['old_name'] if primary else None,
                old_date_planned=primary['old_date_planned'] if primary else None,
                extra_po_lines=extras or None,
                # in dry-run le POL nuove non vengono create -> None
            )

        # === SCRITTURA REALE ===
        new_pol_ids_created = []
        try:
            # 1) Riassegno POL canoni
            for p in consumed_pol_info:
                qty_signed_pol = -1 if is_nota_credito else 1
                self.client._call('purchase.order.line', 'write',
                    [p['po_line_id']], {
                        'price_unit': p['new_price'],
                        'name': p['new_name'],
                        'taxes_id': [(6, 0, [p['new_tax_id']])],
                        'product_qty': 1,
                        'qty_received': qty_signed_pol,
                        'qty_received_manual': qty_signed_pol,
                        'date_planned': invoice_date,
                    })
                logger.info(
                    f"Leasys: updated POL {p['po_line_id']} ({p['voce']}/{p['cls']}) "
                    f"price={p['new_price']:.2f} acc={p['account_id']} tax={p['new_tax_id']}")

            # 2) Creo POL extra ad-hoc
            for info in new_pol_info_list:
                qty_signed_pol = -1 if is_nota_credito else 1
                new_pol_vals = {
                    'order_id': info['order_id'],
                    'name': info['name'],
                    'product_id': info['product_id'],
                    'product_uom': info['product_uom'],
                    'product_qty': info['product_qty'],
                    'price_unit': info['price_unit'],
                    'taxes_id': info['taxes_id'],
                    'date_planned': info['date_planned'],
                    'qty_received': qty_signed_pol,
                    'qty_received_manual': qty_signed_pol,
                }
                if info.get('account_analytic_id'):
                    new_pol_vals['account_analytic_id'] = info['account_analytic_id']
                new_pol_id = self.client._call('purchase.order.line', 'create',
                                                 new_pol_vals)
                if isinstance(new_pol_id, list):
                    new_pol_id = new_pol_id[0] if new_pol_id else None
                if not new_pol_id:
                    raise RuntimeError(f"create POL extra returned empty for {info['_voce']}/{info['_cls']}")
                new_pol_ids_created.append(new_pol_id)
                logger.info(
                    f"Leasys: created NEW POL {new_pol_id} ({info['_voce']}/{info['_cls']}) "
                    f"price={info['price_unit']:.2f} acc={info['_account_id']} tax={info['_tax_id']}")

                # Move_line collegata
                ml_vals = {
                    'name': info['name'],
                    'account_id': info['_account_id'],
                    'price_unit': info['price_unit'],
                    'quantity': qty_signed_pol,
                    'tax_ids': info['taxes_id'],
                    'purchase_line_id': new_pol_id,
                    'product_id': info['product_id'],
                    'product_uom_id': info['product_uom'],
                }
                if info.get('account_analytic_id'):
                    ml_vals['analytic_account_id'] = info['account_analytic_id']
                move_lines_vals.append(ml_vals)

            # 3) Creo il move
            move_vals = dict(move_vals_base)
            move_vals['invoice_line_ids'] = [(0, 0, ml) for ml in move_lines_vals]
            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            if not move_id:
                raise RuntimeError("create move returned empty")
            logger.info(
                f"Created Leasys move {move_id} OdA={oda_name} doc={invoice_number} "
                f"consume-POL={[p['po_line_id'] for p in consumed_pol_info]} "
                f"new-POL={new_pol_ids_created}")

            # 4) Allego XML
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

            # 5) Marca fatturapa registered
            if analysis.attachment_id:
                try:
                    self.client._call('account.move', 'write', [move_id], {
                        'fatturapa_attachment_in_id': analysis.attachment_id,
                    })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                except Exception as e:
                    logger.warning(f"Collegamento fatturapa fallito: {e}")

            if targhe_unmapped:
                logger.warning(
                    f"Leasys: {len(targhe_unmapped)} targhe NON in PARCO "
                    f"(fallback uso_promiscuo): {sorted(targhe_unmapped)[:10]}. "
                    f"Aggiornare input/Parco Auto.xlsx.")

            primary = consumed_pol_info[0] if consumed_pol_info else None
            extras = [
                {'po_line_id': p['po_line_id'],
                 'old_price_unit': p['old_price_unit'],
                 'old_name': p['old_name'],
                 'old_date_planned': p['old_date_planned'],
                 'old_taxes_id': p['old_taxes_id'],
                 'cls': f"{p['voce']}/{p['cls']}"}
                for p in consumed_pol_info[1:]
            ]
            return WriteResult(
                success=True, action='create_draft_automezzi',
                move_id=move_id, dry_run=False,
                po_line_id=primary['po_line_id'] if primary else None,
                old_price_unit=primary['old_price_unit'] if primary else None,
                old_name=primary['old_name'] if primary else None,
                old_date_planned=primary['old_date_planned'] if primary else None,
                extra_po_lines=extras or None,
                added_po_line_ids=new_pol_ids_created or None,
            )

        except Exception as e:
            logger.exception("Errore _create_bozza_leasys_aggregated")
            # Rollback POL canoni riassegnate
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
            # Rollback POL extra create
            for pol_id in new_pol_ids_created:
                try:
                    self.client._call('purchase.order.line', 'unlink', [pol_id])
                except Exception:
                    logger.warning(f"Unlink new POL {pol_id} fallito")
            return WriteResult(success=False, action='create_draft_automezzi',
                               error_message=str(e), dry_run=False)

    # === Factoring: SARDA FACTORING / MBFACTA === #

    def create_bozza_factoring(self, analysis, mapping_entry: Dict) -> WriteResult:
        """
        Per fornitori factoring (flag 'factoring' in MAPPATURA_FORNITORI_FISSI).

        Peculiarità (vs gli altri writer fornitore-fisso):
        - L'OdA-ledger (es. P03522) NON ha righe libere pre-create: la contabilità
          aggiunge una nuova POL per ogni voce. Qui CREIAMO le POL (no consumo).
        - Le righe XML sono tutte IVA 0%, ma con natura diversa: esenti N4
          (taxes_esente, default) tranne il bollo "RECUPERO IMPOSTA DI BOLLO"
          che è N1 art.15 (taxes_bollo). Distinzione per keyword 'BOLLO'.
        - Conto unico (conto_contabile_id), prodotto e analytic fissi da mapping.

        Strategia: raggruppa le righe XML per IVA → max 2 POL nuove su OdA
        (1 esente con somma, 1 bollo con somma). Descrizione di ciascuna POL =
        concatenazione ' - ' delle descrizioni XML del gruppo. move in_invoice.
        Solo TD01/TD24/TD25 (NC inesistenti per questi fornitori → manuali).
        Ritorna added_po_line_ids per il rollback (unlink delle POL create).
        """
        # Validazione mappatura factoring (campi diversi dal fornitore fisso std)
        required = ['oda_fisso', 'partner_id', 'conto_contabile_id', 'product_id',
                    'analytic_account_id', 'taxes_esente', 'journal_id', 'company_id']
        for k in required:
            if k not in mapping_entry or mapping_entry[k] is None:
                return WriteResult(success=False, action='create_draft_factoring',
                                   error_message=f"Mappatura factoring incompleta: "
                                                 f"campo '{k}' mancante",
                                   dry_run=self.dry_run)

        if not analysis.xml_data:
            return WriteResult(success=False, action='create_draft_factoring',
                               error_message="Analysis senza xml_data",
                               dry_run=self.dry_run)

        tipo_doc = analysis.xml_data.tipo_documento or ''
        if tipo_doc not in ('TD01', 'TD24', 'TD25'):
            return WriteResult(success=False, action='create_draft_factoring',
                               error_message=f"Tipo documento {tipo_doc} non supportato "
                                            f"per factoring (solo TD01; le NC vanno "
                                            f"registrate manualmente)",
                               dry_run=self.dry_run)

        # Recupero OdA-ledger
        oda_name = mapping_entry['oda_fisso']
        po = self.client.search_purchase_order_by_name(oda_name)
        if not po:
            return WriteResult(success=False, action='create_draft_factoring',
                               error_message=f"OdA {oda_name} non trovato",
                               dry_run=self.dry_run)
        if po.get('state') != 'purchase':
            return WriteResult(success=False, action='create_draft_factoring',
                               error_message=f"OdA {oda_name} non in stato 'purchase' "
                                            f"(è '{po.get('state')}')",
                               dry_run=self.dry_run)
        po_id = po['id']

        # Raggruppo le righe XML per natura IVA (bollo vs esente)
        righe = analysis.xml_data.righe or []
        if not righe:
            return WriteResult(success=False, action='create_draft_factoring',
                               error_message="Fattura factoring senza righe",
                               dry_run=self.dry_run)

        taxes_esente = mapping_entry['taxes_esente']
        taxes_bollo = mapping_entry.get('taxes_bollo', [47])
        esente_lines, bollo_lines = [], []
        for r in righe:
            if 'BOLLO' in (r.descrizione or '').upper():
                bollo_lines.append(r)
            else:
                esente_lines.append(r)

        def _group_spec(glines, taxes_id):
            amount = round(sum(float(r.prezzo_totale or 0) for r in glines), 2)
            descr = " - ".join(
                r.descrizione.strip() for r in glines
                if (r.descrizione or '').strip()
            ) or 'Commissioni factoring'
            return {'taxes_id': taxes_id, 'amount': amount, 'descr': descr}

        pol_specs = []
        if esente_lines:
            pol_specs.append(_group_spec(esente_lines, taxes_esente))
        if bollo_lines:
            pol_specs.append(_group_spec(bollo_lines, taxes_bollo))

        # Quadratura: somma POL vs imponibile XML (factoring esente: imp == totale)
        total_imponibile = round(sum(p['amount'] for p in pol_specs), 2)
        imponibile_xml = round(float(analysis.xml_data.imponibile_totale or 0), 2)
        if abs(total_imponibile - imponibile_xml) > 0.02:
            return WriteResult(success=False, action='create_draft_factoring',
                               error_message=f"Discrepanza imponibile: somma righe "
                                            f"€{total_imponibile} vs XML €{imponibile_xml}. "
                                            f"Verificare parsing righe.",
                               dry_run=self.dry_run)

        invoice_date = analysis.xml_data.data or ''
        invoice_number = analysis.xml_data.numero or ''
        conto_id = mapping_entry['conto_contabile_id']
        product_id = mapping_entry['product_id']
        analytic_id = mapping_entry['analytic_account_id']
        product_uom = 1  # Units (prod 12301 ha uom_id=1)

        # === DRY RUN ===
        if self.dry_run:
            logger.info(
                f"[DRY_RUN] create_bozza_factoring {oda_name} "
                f"({mapping_entry.get('nome')}): {len(pol_specs)} POL da creare → "
                + "; ".join(
                    f"'{p['descr'][:45]}' €{p['amount']:.2f} tax{p['taxes_id']}"
                    for p in pol_specs)
                + f" | move in_invoice ref={invoice_number} "
                  f"tot imponibile €{total_imponibile:.2f}"
            )
            return WriteResult(success=True, action='create_draft_factoring',
                               move_id=None, dry_run=True)

        # === SCRITTURA REALE ===
        created_pol_ids = []
        try:
            move_lines_vals = []
            for p in pol_specs:
                pol_vals = {
                    'order_id': po_id,
                    'name': p['descr'],
                    'product_id': product_id,
                    'product_uom': product_uom,
                    'product_qty': 1,
                    'price_unit': p['amount'],
                    'qty_received': 1,
                    'qty_received_manual': 1,
                    'taxes_id': [(6, 0, p['taxes_id'])],
                    'date_planned': invoice_date,
                    'account_analytic_id': analytic_id,
                }
                new_pol_id = self.client._call('purchase.order.line', 'create',
                                               pol_vals)
                if isinstance(new_pol_id, list):
                    new_pol_id = new_pol_id[0] if new_pol_id else None
                if not new_pol_id:
                    raise RuntimeError("create POL factoring returned empty")
                created_pol_ids.append(new_pol_id)
                logger.info(f"Factoring: created POL {new_pol_id} on {oda_name} "
                           f"price={p['amount']:.2f} tax={p['taxes_id']} "
                           f"desc='{p['descr'][:50]}'")

                move_lines_vals.append({
                    'name': p['descr'],
                    'quantity': 1,
                    'price_unit': p['amount'],
                    'account_id': conto_id,
                    'tax_ids': [(6, 0, p['taxes_id'])],
                    'purchase_line_id': new_pol_id,
                    'product_id': product_id,
                    'product_uom_id': product_uom,
                    'analytic_account_id': analytic_id,
                })

            payment_term_id = self._get_partner_payment_term(
                mapping_entry['partner_id'])
            data_contabile = self._data_contabile(analysis, invoice_date)
            data_competenza_iva = self._end_of_month(invoice_date)

            move_vals = {
                'partner_id': mapping_entry['partner_id'],
                'move_type': 'in_invoice',
                'invoice_date': invoice_date,
                'date': data_contabile,
                'l10n_it_vat_settlement_date': data_competenza_iva,
                'ref': invoice_number,
                'invoice_origin': oda_name,
                'journal_id': mapping_entry['journal_id'],
                'company_id': mapping_entry['company_id'],
                'invoice_line_ids': [(0, 0, ml) for ml in move_lines_vals],
            }
            if payment_term_id:
                move_vals['invoice_payment_term_id'] = payment_term_id

            move_id = self.client._call('account.move', 'create', move_vals)
            if isinstance(move_id, list):
                move_id = move_id[0] if move_id else None
            logger.info(f"Factoring: created account.move id={move_id} [in_invoice] "
                       f"{len(move_lines_vals)} righe, tot €{total_imponibile:.2f}")

            # Collego e registro l'attachment
            if analysis.attachment_id and move_id:
                try:
                    self.client._call('account.move', 'write', [move_id], {
                        'fatturapa_attachment_in_id': analysis.attachment_id,
                    })
                    self.client._call('fatturapa.attachment.in', 'write',
                        [analysis.attachment_id], {'registered': True})
                    logger.info(f"Collegato attachment {analysis.attachment_id} "
                               f"al move {move_id} (registered=True)")
                except Exception as e:
                    logger.warning(f"Collegamento attachment fallito "
                                  f"(non blocca): {e}")

            # Allego XML
            if analysis.raw_xml and move_id:
                try:
                    self.client._call('ir.attachment', 'create', {
                        'name': f"{invoice_number}.xml",
                        'datas': base64.b64encode(
                            analysis.raw_xml.encode('utf-8')).decode('ascii'),
                        'res_model': 'account.move',
                        'res_id': move_id,
                        'mimetype': 'application/xml',
                    })
                    logger.info(f"XML allegato a move {move_id}")
                except Exception as e:
                    logger.warning(f"Allegato XML fallito (non blocca): {e}")

            return WriteResult(success=True, action='create_draft_factoring',
                               move_id=move_id, po_line_id=None,
                               added_po_line_ids=created_pol_ids or None,
                               dry_run=False)

        except Exception as e:
            logger.exception("Errore create_bozza_factoring")
            # Rollback: unlink delle POL appena create
            for pol_id in created_pol_ids:
                try:
                    self.client._call('purchase.order.line', 'unlink', [pol_id])
                    logger.info(f"Rollback: unlink POL factoring {pol_id}")
                except Exception:
                    logger.warning(f"Unlink POL factoring {pol_id} fallito")
            return WriteResult(success=False, action='create_draft_factoring',
                               error_message=str(e), dry_run=False)
