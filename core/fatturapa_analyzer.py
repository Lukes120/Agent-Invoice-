"""
Analyzer Fase 1: processa gli allegati FatturaPA non ancora registrati,
estrae dati dall'XML, cerca OdA in Odoo, esegue matching.

Output: per ogni fattura una classificazione con motivo e azione suggerita.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import (
    parse_from_base64, FatturaPAData, FatturaPALine,
)
from core.matcher import InvoiceMatcher, LineMatch
from core.keyword_rules import classify_line_by_keyword

logger = logging.getLogger(__name__)


@dataclass
class FatturaPAAnalysis:
    """Risultato dell'analisi di un allegato FatturaPA."""
    attachment_id: int
    attachment_name: str = ""
    supplier_name: str = ""
    supplier_vat: str = ""
    invoice_number: str = ""
    invoice_date: str = ""
    invoice_total: float = 0.0

    # Data di creazione del record fatturapa.attachment.in in Odoo, equivalente
    # alla data di ricezione dallo SdI. Usata dai writer per popolare la
    # 'data contabile' (`account.move.date`) e la 'data competenza IVA'
    # (`l10n_it_vat_settlement_date`). Formato Odoo datetime: 'YYYY-MM-DD HH:MM:SS'.
    attachment_create_date: str = ""

    # Dati estratti dall'XML
    xml_data: Optional[FatturaPAData] = None
    raw_xml: str = ""  # XML grezzo, usato da odoo_writer per estrazioni aggiuntive

    # OdA trovato
    oda_references_in_xml: List[str] = field(default_factory=list)
    oda_values_raw: List[str] = field(default_factory=list)
    purchase_order: Optional[Dict] = None   # OdA Odoo se trovato
    po_lines: List[Dict] = field(default_factory=list)

    # Commesse rilevate (per Fase 2 futura)
    commesse_detected: List[str] = field(default_factory=list)

    # Inconsistenze segnalate da Odoo
    odoo_inconsistencies: str = ""

    # Match riga per riga
    line_matches: List[LineMatch] = field(default_factory=list)

    # Totali
    total_diff: float = 0.0
    total_diff_percent: float = 0.0

    # Analisi cumulativa (somma fatture su stesso OdA)
    cumulative_other_invoices: float = 0.0       # somma fatture ALTRE per lo stesso OdA
    cumulative_other_count: int = 0              # quante altre fatture
    cumulative_total_with_current: float = 0.0   # somma tutte fatture INCLUSA corrente
    cumulative_vs_po_diff: float = 0.0           # cumulato - OdA
    cumulative_vs_po_percent: float = 0.0        # scostamento cumulato %

    # Match implicito (OdA dedotto da fornitore + importo)
    implicit_match_candidates: List[Dict] = field(default_factory=list)
    implicit_match_applied: bool = False         # se un match implicito è stato usato
    implicit_match_used_loose: bool = False      # se è stata usata la tolleranza larga
    # Evidence label: 'amount_only' (solo importo) | 'amount+strong' (con cod/desc)
    implicit_match_evidence: str = ""
    # Pattern OdA-ledger: line_ids selezionati da _try_oda_ledger_subset_match
    # quando MATCH_PARZIALE_OK e' assegnato dentro CASO 3 (OdA esplicito).
    partial_match_subset_lines: List[int] = field(default_factory=list)

    # Match parziale (OdA dedotto da sottoinsieme righe + extra)
    partial_match_applied: bool = False
    partial_extra_lines: List[Dict] = field(default_factory=list)   # righe escluse dal match
    partial_extra_total: float = 0.0

    # Suggerimenti OdA (non assegnati, solo proposti)
    # Usati quando la fattura cade in NO_ODA ma esistono OdA aperti del
    # fornitore con sottoinsiemi di righe che matchano l'importo fattura.
    suggested_pos: List[Dict] = field(default_factory=list)

    # Classificazione finale
    classification: str = "ANOMALIA"
    priority_score: float = 0.0
    actions_suggested: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# Categorie possibili nella Fase 1:
# AUTO_VALIDABILE          = OdA esplicito, singola fattura entro tolleranza
# MATCH_IMPLICITO          = OdA dedotto da fornitore+importo (1 candidato)
# MATCH_IMPLICITO_AMBIGUO  = OdA dedotto ma 2+ candidati, scelta manuale
# MATCH_PARZIALE_OK        = OdA + righe extra (sottoinsieme matcha OdA)
# MATCH_PARZIALE_AMBIGUO   = pi OdA candidati per il match parziale
# PARZIALE_CUMULATIVO_OK   = fattura parziale, cumulato con altre arriva a OdA
# TRASPORTO_OK             = solo righe spese accessorie (anche senza OdA)
# DA_VERIFICARE            = OdA trovato ma differenze oltre tolleranza
# CUMULATIVO_ECCEDE        = somma fatture supera significativamente l'OdA
# ODA_RIFERITO_NON_TROVATO = OdA citato in XML ma non esiste in Odoo
# NO_ODA_DA_CLASSIFICARE   = nessun OdA, nessun match (implicito o parziale)
# NO_ODA_CON_SUGGERIMENTI  = nessun OdA trovato ma 2+ OdA aperti del fornitore
#                            con righe matchanti l'imponibile (ambiguo,
#                            registrazione manuale guidata)
# MATCH_DA_SUGGERIMENTO    = nessun OdA in XML, ma 1 SOLO OdA aperto del
#                            fornitore ha sottoinsieme righe = imponibile
#                            (univoco al centesimo, bozza creabile con
#                            verifica manuale prima del posting)
# MATCH_DA_SUGGERIMENTO_PIU_EXTRA = come MATCH_DA_SUGGERIMENTO ma con un
#                            piccolo scostamento (spese accessorie tipo
#                            trasporto). Il writer aggiunge una riga
#                            'spese accessorie' col delta sul conto 420110.
# COMMESSA_DETECTED        = rilevata commessa S##### (per Fase 2)
# ANOMALIA                 = errore parsing / altri problemi


class FatturaPAAnalyzer:
    """Orchestratore dell'analisi di una fattura elettronica."""

    def __init__(self, client: OdooReadOnlyClient, matcher: InvoiceMatcher,
                 tol_total: float,
                 implicit_match_enabled: bool = True,
                 implicit_match_tolerance_percent: float = 0.0,
                 implicit_match_tolerance_absolute: float = 0.01,
                 implicit_match_loose_tolerance_absolute: float = 2.00,
                 implicit_match_loose_tolerance_percent: float = 0.5,
                 implicit_match_duplicate_guard: bool = True,
                 partial_match_enabled: bool = True,
                 partial_match_max_rows: int = 12,
                 partial_match_max_extra_percent: float = 50.0,
                 partial_match_tolerance_absolute: float = 0.01,
                 suggestions_enabled: bool = True,
                 suggestions_max_lines: int = 40,
                 suggestions_tolerance_absolute: float = 0.01,
                 suggestions_max_age_months: int = 12,
                 supplier_mapping_enabled: bool = True,
                 supplier_mapping: Optional[Dict] = None):
        self.client = client
        self.matcher = matcher
        self.tol_total = tol_total
        self.implicit_match_enabled = implicit_match_enabled
        self.implicit_match_tolerance_percent = implicit_match_tolerance_percent
        self.implicit_match_tolerance_absolute = implicit_match_tolerance_absolute
        self.implicit_match_loose_tolerance_absolute = implicit_match_loose_tolerance_absolute
        self.implicit_match_loose_tolerance_percent = implicit_match_loose_tolerance_percent
        self.implicit_match_duplicate_guard = implicit_match_duplicate_guard
        self.partial_match_enabled = partial_match_enabled
        self.partial_match_max_rows = partial_match_max_rows
        self.partial_match_max_extra_percent = partial_match_max_extra_percent
        self.partial_match_tolerance_absolute = partial_match_tolerance_absolute
        self.suggestions_enabled = suggestions_enabled
        self.suggestions_max_lines = suggestions_max_lines
        self.suggestions_tolerance_absolute = suggestions_tolerance_absolute
        self.suggestions_max_age_months = suggestions_max_age_months
        self.supplier_mapping_enabled = supplier_mapping_enabled
        self.supplier_mapping = supplier_mapping or {}
        # Cache OdA già cercati per evitare chiamate ripetute
        self._po_cache: Dict[str, Optional[Dict]] = {}
        # Cache match impliciti per partner (ottimizzazione performance)
        self._implicit_cache: Dict[tuple, List[Dict]] = {}
        # Cache OdA aperti per partner (per match parziale)
        self._open_pos_cache: Dict[int, List[Dict]] = {}
        # Cache OdA aperti per partner (per suggerimenti, filtro temporale)
        self._open_pos_cache_recent: Dict[int, List[Dict]] = {}
        # Cache righe OdA (per suggerimenti)
        self._po_lines_cache: Dict[int, List[Dict]] = {}

    def analyze(self, attachment: Dict) -> FatturaPAAnalysis:
        """Analizza un singolo allegato fatturapa.attachment.in."""
        analysis = FatturaPAAnalysis(
            attachment_id=attachment['id'],
            attachment_name=attachment.get('name', ''),
            invoice_total=float(attachment.get('invoices_total', 0) or 0),
            invoice_date=attachment.get('invoices_date', '') or '',
            odoo_inconsistencies=attachment.get('inconsistencies', '') or '',
            attachment_create_date=str(attachment.get('create_date') or ''),
        )

        supplier = attachment.get('xml_supplier_id')
        partner_id = None
        if isinstance(supplier, list) and len(supplier) > 1:
            analysis.supplier_name = supplier[1]
            partner_id = supplier[0]
        # Salvo per uso nel match implicito (non esposto in dataclass perché
        # non serve all'utente)
        analysis._partner_id_odoo = partner_id

        # Se Odoo ha già parsing error, segnalo
        parsing_err = attachment.get('e_invoice_parsing_error')
        if parsing_err:
            analysis.warnings.append(f"Odoo parsing error: {parsing_err}")

        # Decodifico e parso XML
        xml_b64 = attachment.get('datas')
        if not xml_b64:
            analysis.classification = "ANOMALIA"
            analysis.actions_suggested.append("XML mancante nell'allegato")
            return analysis

        xml_data = parse_from_base64(xml_b64)
        analysis.xml_data = xml_data

        # Tengo l'XML grezzo decodificato per usi successivi (odoo_writer)
        try:
            import base64 as _b64
            analysis.raw_xml = _b64.b64decode(xml_b64).decode('utf-8', errors='replace')
        except Exception:
            analysis.raw_xml = ""

        if xml_data.parsing_errors:
            analysis.warnings.extend(xml_data.parsing_errors)

        analysis.supplier_vat = xml_data.cedente_partita_iva
        if not analysis.supplier_name and xml_data.cedente_denominazione:
            analysis.supplier_name = xml_data.cedente_denominazione
        analysis.invoice_number = xml_data.numero
        if not analysis.invoice_total and xml_data.importo_totale:
            analysis.invoice_total = xml_data.importo_totale

        analysis.oda_references_in_xml = list(xml_data.oda_riferimenti)
        analysis.oda_values_raw = list(xml_data.oda_valori_grezzi)
        analysis.commesse_detected = list(xml_data.commessa_riferimenti)

        # Se non ci sono OdA strutturati, provo quelli testuali (descrizioni/causali)
        # Vengono validati contro Odoo: se trovo l'OdA E appartiene allo stesso
        # fornitore, lo considero buono (altrimenti è un falso positivo)
        textual_odas_used = []
        if not analysis.oda_references_in_xml and xml_data.oda_riferimenti_testuali:
            for oda_code in xml_data.oda_riferimenti_testuali:
                candidate_po = self._find_purchase_order([oda_code])
                if not candidate_po:
                    continue
                # Verifico che l'OdA sia dello stesso fornitore
                po_partner = candidate_po.get('partner_id')
                po_partner_id = po_partner[0] if isinstance(po_partner, list) else None
                if partner_id and po_partner_id == partner_id:
                    textual_odas_used.append(oda_code)
                    analysis.oda_references_in_xml.append(oda_code)

            if textual_odas_used:
                analysis.warnings.append(
                    f"OdA trovati nel testo libero (non in tag strutturato): "
                    f"{', '.join(textual_odas_used)}. Validati vs Odoo."
                )

        # === Cerco l'OdA in Odoo ===
        po = self._find_purchase_order(xml_data.oda_riferimenti + textual_odas_used)
        analysis.purchase_order = po
        if po:
            analysis.po_lines = self.client.get_purchase_order_lines(
                po.get('order_line', [])
            )

        # === Match riga per riga ===
        for xml_line in xml_data.righe:
            # Adatto la struttura FatturaPALine a quella che si aspetta il matcher
            inv_line_dict = self._line_to_invoice_dict(xml_line)
            lm = self.matcher.match_line(inv_line_dict, analysis.po_lines)
            analysis.line_matches.append(lm)

        # === Classificazione ===
        self._classify(analysis)

        return analysis

    def _find_purchase_order(self, oda_refs: List[str]) -> Optional[Dict]:
        """
        Cerca un OdA in Odoo con uno dei riferimenti estratti.
        Usa cache per efficienza.
        """
        for ref in oda_refs:
            if ref in self._po_cache:
                if self._po_cache[ref]:
                    return self._po_cache[ref]
                continue
            po = self.client.search_purchase_order_by_name(ref)
            self._po_cache[ref] = po
            if po:
                return po
        return None

    def _line_to_invoice_dict(self, line: FatturaPALine) -> Dict:
        """Converte FatturaPALine in struttura compatibile con matcher."""
        return {
            'id': line.numero_linea,
            'name': line.descrizione,
            'product_id': False,  # XML non contiene il product_id Odoo
            'quantity': line.quantita,
            'price_unit': line.prezzo_unitario,
            'price_subtotal': line.prezzo_totale,
            'purchase_line_id': False,
        }

    def _classify(self, analysis: FatturaPAAnalysis):
        """Applica logica di classificazione specifica Fase 1."""

        line_matches = analysis.line_matches
        keyword_count = sum(1 for lm in line_matches if lm.match_type == "KEYWORD")
        no_match_count = sum(1 for lm in line_matches if lm.match_type == "NO_MATCH")
        tolerance_count = sum(1 for lm in line_matches if lm.match_type == "TOLERANCE")
        exact_count = sum(1 for lm in line_matches if lm.match_type == "EXACT")
        out_tol_count = sum(1 for lm in line_matches if lm.match_type == "OUT_OF_TOLERANCE")
        matched_ok = exact_count + tolerance_count + keyword_count
        total_lines = len(line_matches)

        # === CASO 1: NESSUN OdA nell'XML ===
        if not analysis.oda_references_in_xml:

            # NOTA: la presenza di una commessa S##### è ora trattata come
            # INDIZIO ADDITIVO (post-filtro nei match implicito/parziale),
            # non più come early-exit. La classificazione COMMESSA_DETECTED
            # rimane come fallback finale se nessuno dei tentativi successivi
            # ha esito.

            # Sotto-caso: tutte le righe sono spese accessorie note
            if keyword_count > 0 and no_match_count == 0 and keyword_count == total_lines:
                analysis.classification = "TRASPORTO_OK"
                analysis.actions_suggested.append(
                    f"Fattura senza OdA, tutte le righe classificate come spese "
                    f"accessorie (trasporto/bolli/etc.). "
                    f"Fornitore: {analysis.supplier_name}. Registrare sui conti suggeriti."
                )
                return

            # Sotto-caso: MATCH IMPLICITO - provo a dedurre OdA da
            # fornitore + imponibile identico
            if self.implicit_match_enabled:
                self._try_implicit_match(analysis)
                if analysis.classification != "ANOMALIA":
                    # Il match implicito ha già assegnato una classificazione
                    return

            # Sotto-caso: MATCH PARZIALE - provo a dedurre OdA da
            # sottoinsieme di righe fattura. Ultima risorsa prima di
            # NO_ODA.
            if self.partial_match_enabled:
                self._try_partial_match(analysis)
                if analysis.classification != "ANOMALIA":
                    return

            # Sotto-caso: MAPPATURA FORNITORI FISSI - per fornitori con
            # pattern certo (Trenitalia, Italo, Telecom) applica OdA + conto fissi.
            # PRIMA dei suggerimenti: la mappatura è più affidabile.
            if self.supplier_mapping_enabled and self.supplier_mapping:
                self._try_supplier_fixed_mapping(analysis)
                if analysis.classification in ("MAPPATURA_FORNITORE_FISSO",
                                                "MAPPATURA_AUTOMEZZI"):
                    return

            # Sotto-caso: SUGGERIMENTI - cerco OdA aperti del fornitore
            # con sottoinsiemi di righe che matchano l'imponibile fattura.
            # Se 1 SOLO OdA candidato -> MATCH_DA_SUGGERIMENTO (bozza creabile);
            # se >=2 candidati -> NO_ODA_CON_SUGGERIMENTI (review manuale).
            if self.suggestions_enabled:
                self._try_oda_line_subset_suggestions(analysis)
                if analysis.classification in ("NO_ODA_CON_SUGGERIMENTI",
                                               "MATCH_DA_SUGGERIMENTO",
                                               "MATCH_DA_SUGGERIMENTO_PIU_EXTRA"):
                    return

            # Sotto-caso: rilevata commessa S##### ma nessun match (importo,
            # parziale, mappatura, suggerimenti). L'OdA potrebbe non essere
            # ancora stato creato — pattern "ritiro al banco / fattura differita"
            # dove Acquisti emette l'OdA dopo la fattura.
            if analysis.commesse_detected:
                analysis.classification = "COMMESSA_DETECTED"
                analysis.actions_suggested.append(
                    f"Nessun OdA aperto del fornitore con importo coincidente. "
                    f"Rilevata commessa di vendita "
                    f"{', '.join(analysis.commesse_detected)}: probabile OdA "
                    f"non ancora creato in Odoo (pattern ritiro al banco). "
                    f"Ricontrollare nelle prossime run."
                )
                return

            # Sotto-caso generale: fornitore senza OdA (utenze, leasing, ecc.)
            analysis.classification = "NO_ODA_DA_CLASSIFICARE"
            hint = ""
            if analysis.oda_values_raw:
                hint = f" Valori grezzi nell'XML: {analysis.oda_values_raw}."
            analysis.actions_suggested.append(
                f"Nessun riferimento OdA standard (P+5cifre) trovato nell'XML. "
                f"Fornitore candidato per Fase 3 (tabella fornitori fissi).{hint}"
            )
            return

        # === CASO 2: OdA citato in XML ma non trovato in Odoo ===
        if analysis.purchase_order is None:
            analysis.classification = "ODA_RIFERITO_NON_TROVATO"
            analysis.actions_suggested.append(
                f"Riferimento OdA {analysis.oda_references_in_xml} citato "
                f"nell'XML ma non trovato in Odoo. "
                f"Verificare se l'ordine esiste davvero o è stato scritto "
                f"sbagliato dal fornitore."
            )
            return

        # === CASO 3: OdA trovato, analisi basata su totale imponibile ===
        # Nota: il matching riga-per-riga con l'XML FatturaPA non è affidabile
        # perché l'XML non contiene product_id Odoo. Ci basiamo sul confronto
        # dell'imponibile totale, usando le righe solo per riconoscere
        # le spese accessorie (keyword) ed escluderle dal confronto.

        # FIX A (RAJAPACK pattern): se una riga keyword è già coperta da una POL
        # accessoria dell'OdA con stesso importo (es. POL "spese" 13,50 €), NON
        # va trattata come extra — riclassifico a EXACT prima del calcolo
        # keyword_amount, così la fattura cade in AUTO_VALIDABILE.
        po_lines_for_cover = getattr(analysis, 'po_lines', None) or []
        if po_lines_for_cover:
            accessory_kws = ('spes', 'trasport', 'spedizion', 'bollo', 'bolli',
                             'oneri', 'imballag', 'noleg')
            keyword_lms = [lm for lm in line_matches if lm.match_type == "KEYWORD"]
            covering_pols = [pol for pol in po_lines_for_cover
                             if float(pol.get('qty_invoiced') or 0) <= 0.001
                             and any(k in (pol.get('name') or '').lower()
                                     for k in accessory_kws)]
            used_pol_ids = set()
            cover_map = []  # [(LineMatch, pol_dict)] da riclassificare
            for lm in keyword_lms:
                kw_amount = float(lm.invoice_line.get('price_subtotal') or 0)
                if kw_amount <= 0:
                    continue
                for pol in covering_pols:
                    if pol['id'] in used_pol_ids:
                        continue
                    if abs(float(pol.get('price_subtotal') or 0) - kw_amount) < 0.01:
                        cover_map.append((lm, pol))
                        used_pol_ids.add(pol['id'])
                        break
            # Riclassifico solo se TUTTE le keyword sono coperte 1-a-1 da POL OdA
            if cover_map and len(cover_map) == len(keyword_lms):
                for lm, pol in cover_map:
                    lm.match_type = "EXACT"
                    lm.po_line = pol
                    lm.keyword_category = None
                    lm.keyword_account = None
                    lm.notes.append(
                        f"Coperta da POL OdA '{(pol.get('name') or '')[:40]}' "
                        f"(€{float(pol.get('price_subtotal') or 0):.2f}) — "
                        f"non trattata come extra"
                    )
                keyword_count = 0
                exact_count += len(cover_map)

        # Somma delle righe classificate come keyword (trasporto, bolli...)
        # queste sono "extra" che in genere non sono nell'OdA
        keyword_amount = sum(
            float(lm.invoice_line.get('price_subtotal', 0))
            for lm in line_matches if lm.match_type == "KEYWORD"
        )

        inv_untaxed = float(analysis.xml_data.imponibile_totale or 0)
        po_untaxed = float(analysis.purchase_order.get('amount_untaxed', 0))

        # SANITY CHECK: se le righe keyword coprono praticamente tutta la fattura,
        # probabilmente sono FALSI POSITIVI (es. "supporto" che matcha "porto").
        # Le spese accessorie vere sono sempre una frazione minoritaria.
        if keyword_amount > 0 and inv_untaxed > 0:
            keyword_share = keyword_amount / inv_untaxed
            if keyword_share > 0.5:
                analysis.warnings.append(
                    f"Keyword hanno catturato {keyword_share*100:.0f}% dell'imponibile. "
                    f"Probabile falso positivo: le righe NON verranno trattate "
                    f"come spese accessorie."
                )
                keyword_amount = 0
                for lm in line_matches:
                    if lm.match_type == "KEYWORD":
                        lm.match_type = "NO_MATCH"
                        lm.keyword_category = None
                        lm.keyword_account = None
                        lm.notes.append(
                            "Riclassificata (keyword risultava falso positivo)"
                        )
                keyword_count = 0

        # Diff "singola fattura vs OdA" (per riferimento)
        analysis.total_diff = (inv_untaxed - keyword_amount) - po_untaxed
        if po_untaxed:
            analysis.total_diff_percent = abs(analysis.total_diff / po_untaxed * 100)

        # === LOGICA CUMULATIVA ===
        # Calcolo la somma delle ALTRE fatture già esistenti su questo OdA
        po_name = analysis.purchase_order.get('name')
        po_id = analysis.purchase_order.get('id')
        cumulative_info = self.client.get_invoiced_amount_for_po(po_id, po_name)

        # Escludo eventuali "altre" che sarebbero in realtà la fattura corrente
        # (l'allegato che stiamo analizzando potrebbe avere già generato una move
        # collegata: in genere NO perché stiamo processando NON registrate, ma per
        # prudenza filtriamo). Per ora consideriamo tutte le fatture trovate come
        # "altre" rispetto alla corrente in analisi.
        other_invoiced = (
            cumulative_info['already_invoiced_posted']
            + cumulative_info['already_invoiced_draft']
        )
        analysis.cumulative_other_invoices = other_invoiced
        analysis.cumulative_other_count = cumulative_info['count_invoices']

        # Totale cumulato INCLUSA fattura corrente
        # (sottraggo keyword perché sono extra non comparabili con OdA)
        cumulative_total = other_invoiced + (inv_untaxed - keyword_amount)
        analysis.cumulative_total_with_current = cumulative_total
        analysis.cumulative_vs_po_diff = cumulative_total - po_untaxed
        if po_untaxed:
            analysis.cumulative_vs_po_percent = abs(
                analysis.cumulative_vs_po_diff / po_untaxed * 100
            )

        # Tolleranze
        within_absolute = abs(analysis.total_diff) <= self.tol_total
        within_percent = analysis.total_diff_percent <= self.matcher.tol_percent
        cumulative_within_absolute = abs(analysis.cumulative_vs_po_diff) <= self.tol_total
        cumulative_within_percent = analysis.cumulative_vs_po_percent <= self.matcher.tol_percent

        po_state = analysis.purchase_order.get('state')

        # Warning se OdA in stato draft
        if po_state == 'draft':
            analysis.warnings.append(
                f"OdA {po_name} ancora in stato BOZZA (non confermato)"
            )

        # ==== CLASSIFICAZIONE ====
        # Caso più semplice: fattura singola = OdA
        if abs(analysis.total_diff) < 0.01:
            if keyword_count > 0:
                # Pattern Sonepar/RemaTarlazzi: merci quadrano con OdA, ma la
                # fattura ha N righe spese accessorie extra. Classifico come
                # MATCH_PARZIALE_OK così create_bozza_da_oda_matched chiama
                # _add_extra_pol_to_oda per aggiungere le POL accessorie sull'OdA
                # (conto/product da EXTRA_POL_MAPPING_ECOTEL).
                analysis.classification = "MATCH_PARZIALE_OK"
                analysis.partial_extra_lines = self._build_extra_lines_from_keyword_matches(analysis)
                analysis.partial_extra_total = keyword_amount
                analysis.partial_match_applied = True
                analysis.actions_suggested.append(
                    f"OdA {po_name}: merci €{inv_untaxed-keyword_amount:.2f} "
                    f"matcha l'OdA + {keyword_count} righe spese accessorie "
                    f"€{keyword_amount:.2f} aggiunte come POL extra. Registrare."
                )
            else:
                analysis.classification = "AUTO_VALIDABILE"
                analysis.actions_suggested.append(
                    f"OdA {po_name}: imponibile identico (€{inv_untaxed:.2f}). Registrare."
                )
            return

        # Fattura singola entro tolleranza
        if within_absolute or within_percent:
            if keyword_count > 0:
                analysis.classification = "MATCH_PARZIALE_OK"
                analysis.partial_extra_lines = self._build_extra_lines_from_keyword_matches(analysis)
                analysis.partial_extra_total = keyword_amount
                analysis.partial_match_applied = True
                analysis.actions_suggested.append(
                    f"OdA {po_name}: merci €{inv_untaxed-keyword_amount:.2f} vs "
                    f"OdA €{po_untaxed:.2f}, diff €{analysis.total_diff:+.2f} "
                    f"({analysis.total_diff_percent:.1f}%) entro tolleranza. "
                    f"{keyword_count} righe spese accessorie €{keyword_amount:.2f} "
                    f"aggiunte come POL extra. Registrare."
                )
            else:
                analysis.classification = "AUTO_VALIDABILE"
                analysis.actions_suggested.append(
                    f"OdA {po_name}: imponibile €{inv_untaxed:.2f} vs OdA €{po_untaxed:.2f}, "
                    f"diff €{analysis.total_diff:+.2f} ({analysis.total_diff_percent:.1f}%) "
                    f"entro tolleranza. Registrare."
                )
            return

        # Ci sono altre fatture esistenti sull'OdA? Applico logica cumulativa
        if analysis.cumulative_other_count > 0:

            # Il cumulato è sotto il totale OdA (inclusa la corrente)?
            # Cioè: cumulato <= OdA (con tolleranza) → fatturazione parziale in corso
            if analysis.cumulative_vs_po_diff <= self.tol_total:
                # Sotto o ~uguale al totale OdA
                residuo = po_untaxed - cumulative_total
                analysis.classification = "PARZIALE_CUMULATIVO_OK"
                analysis.actions_suggested.append(
                    f"OdA {po_name}: fatturazione parziale/multipla. "
                    f"Già fatturato da {analysis.cumulative_other_count} altre fatture: "
                    f"€{other_invoiced:.2f}. Corrente: €{inv_untaxed:.2f}. "
                    f"Totale cumulato: €{cumulative_total:.2f} / OdA €{po_untaxed:.2f} "
                    f"(residuo €{residuo:.2f}). Registrare come parziale."
                )
                return

            # Il cumulato è entro tolleranza dell'OdA? Completiamo l'OdA
            if cumulative_within_absolute or cumulative_within_percent:
                analysis.classification = "PARZIALE_CUMULATIVO_OK"
                analysis.actions_suggested.append(
                    f"OdA {po_name}: fattura chiude l'OdA. "
                    f"Già fatturato: €{other_invoiced:.2f} ({analysis.cumulative_other_count} fatt.), "
                    f"corrente: €{inv_untaxed:.2f}, totale €{cumulative_total:.2f} "
                    f"vs OdA €{po_untaxed:.2f} (diff €{analysis.cumulative_vs_po_diff:+.2f}). "
                    f"Registrare."
                )
                return

            # Il cumulato eccede significativamente l'OdA → attenzione reale
            analysis.classification = "CUMULATIVO_ECCEDE"
            analysis.actions_suggested.append(
                f"ATTENZIONE OdA {po_name}: il cumulato €{cumulative_total:.2f} "
                f"SUPERA l'OdA €{po_untaxed:.2f} di €{analysis.cumulative_vs_po_diff:.2f} "
                f"({analysis.cumulative_vs_po_percent:.1f}%). "
                f"Già fatturato in {analysis.cumulative_other_count} altre fatture: "
                f"€{other_invoiced:.2f}. Verificare se c'è sovra-fatturazione "
                f"o se l'OdA va aggiornato."
            )
            return

        # Nessun'altra fattura sull'OdA: prima di concludere DA_VERIFICARE,
        # tento il pattern OdA-ledger (la fattura consuma un subset di righe
        # libere dell'OdA, es. RWS Tech: 1 rata su 12, Trenitalia/Italo style
        # ma per qualsiasi fornitore senza mappatura esplicita).
        if (analysis.total_diff < 0 and
                self._try_oda_ledger_subset_match(analysis, inv_untaxed, po_untaxed)):
            return

        # Scostamento singola fattura vs OdA: fallback DA_VERIFICARE
        if analysis.total_diff > 0 and keyword_count == 0:
            analysis.classification = "DA_VERIFICARE"
            analysis.actions_suggested.append(
                f"OdA {po_name}: fattura €{inv_untaxed:.2f} supera OdA €{po_untaxed:.2f} "
                f"di €{analysis.total_diff:.2f} ({analysis.total_diff_percent:.1f}%). "
                f"Possibili righe extra (trasporto/spese) non riconosciute automaticamente."
            )
        else:
            analysis.classification = "DA_VERIFICARE"
            direction = "superiore" if analysis.total_diff > 0 else "inferiore"
            analysis.actions_suggested.append(
                f"OdA {po_name}: fattura €{inv_untaxed:.2f} {direction} a OdA "
                f"€{po_untaxed:.2f}, scostamento €{abs(analysis.total_diff):.2f} "
                f"({analysis.total_diff_percent:.1f}%) oltre tolleranza. "
                f"Verificare prezzi/quantità con il fornitore "
                f"(nessuna altra fattura trovata sullo stesso OdA)."
            )

    def _build_extra_lines_from_keyword_matches(self, analysis: 'FatturaPAAnalysis') -> List[Dict]:
        """
        Costruisce la struttura partial_extra_lines (consumata dal writer in
        _add_extra_pol_to_oda) a partire dalle line_matches di tipo KEYWORD,
        risalendo alle FatturaPALine originali in analysis.xml_data.righe per
        recuperare i campi mancanti (aliquota_iva, unita_misura, codice_articolo).

        Schema identico a quello popolato in _try_partial_match.
        """
        xml_lines_by_id = {l.numero_linea: l for l in (analysis.xml_data.righe or [])}
        extra = []
        for lm in analysis.line_matches:
            if lm.match_type != 'KEYWORD':
                continue
            line_num = lm.invoice_line.get('id')
            original = xml_lines_by_id.get(line_num)
            if not original:
                continue
            extra.append({
                'descrizione': original.descrizione,
                'quantita': original.quantita,
                'prezzo': original.prezzo_totale,
                'prezzo_totale': original.prezzo_totale,
                'prezzo_unitario': original.prezzo_unitario,
                'aliquota_iva': original.aliquota_iva,
                'unita_misura': original.unita_misura,
                'codice_articolo_valore': getattr(original, 'codice_articolo_valore', ''),
            })
        return extra

    def _try_oda_ledger_subset_match(self, analysis: 'FatturaPAAnalysis',
                                      inv_untaxed: float, po_untaxed: float) -> bool:
        """
        Pattern OdA-ledger: l'OdA esplicito ha N righe libere e la fattura ne
        consuma 1 o piu' (es. RWS Tech 1/12 rate, Trenitalia-style ma senza
        mappatura esplicita).

        Riusa _find_subset_match per cercare un sottoinsieme di righe libere
        OdA che sommi (inv_untaxed - keyword_amount) entro la tolleranza
        stretta. Le righe XML keyword-classified (TRASPORTO/ONERI_BANCARI/
        BOLLO) vengono escluse dal target perche' tipicamente NON sono
        modellate sull'OdA: vengono invece girate al writer come
        partial_extra_lines, cosi' _add_extra_pol_to_oda le aggiungera'
        come POL extra accessorie.

        Si attiva SOLO se:
        - analysis.purchase_order popolato e analysis.po_lines disponibili
        - target (merci nette) > 0 e <= po_untaxed
        - esistono POL libere

        Ritorna True se ha promosso a MATCH_PARZIALE_OK; False se nessun
        match valido (caller continuera' col fallback DA_VERIFICARE).
        """
        po_lines = getattr(analysis, 'po_lines', None) or []
        if not po_lines:
            return False

        # P1: scorporo le righe accessorie (keyword) dal target subset.
        # Le accessorie verranno appese come POL extra al momento della bozza.
        keyword_amount = sum(
            float(lm.invoice_line.get('price_subtotal', 0))
            for lm in analysis.line_matches if lm.match_type == "KEYWORD"
        )
        target = inv_untaxed - keyword_amount

        tol = self.partial_match_tolerance_absolute
        if target <= 0 or po_untaxed < target - tol:
            return False

        # Conta righe "libere" disponibili (qty_invoiced=0). Se non ce ne sono,
        # niente da fare (caso "tutto gia' fatturato": e' un vero DA_VERIFICARE).
        libere = [l for l in po_lines
                  if float(l.get('product_qty') or 0) > 0
                  and float(l.get('qty_invoiced') or 0) <= 0.001
                  and float(l.get('price_subtotal') or 0) > 0]
        if not libere:
            return False

        # _find_subset_match restituisce un dict {count, type, desc, line_ids,
        # extra_amount} se il subset e' trovato. Riusa la primitiva esistente.
        match_info = self._find_subset_match(libere, target, tol,
                                             secondary_tolerance=0.0)
        if not match_info:
            return False
        if match_info.get('extra_amount', 0) > tol:
            # Match solo grazie a tolleranza secondaria: prudente, lascia DA_VERIFICARE
            return False

        # Match valido: classifico come MATCH_PARZIALE_OK con dettagli ledger.
        line_ids = match_info.get('line_ids') or []
        n_libere_tot = len(libere)
        po_name = analysis.purchase_order.get('name', '')
        analysis.classification = "MATCH_PARZIALE_OK"
        # Salvo i line_ids selezionati per uso del writer
        analysis.partial_match_subset_lines = line_ids

        # P1: se ci sono righe accessorie, popolo partial_extra_lines per il
        # writer (_add_extra_pol_to_oda gia' in produzione).
        kw_count = sum(1 for lm in analysis.line_matches if lm.match_type == "KEYWORD")
        if keyword_amount > 0 and kw_count > 0:
            analysis.partial_extra_lines = self._build_extra_lines_from_keyword_matches(analysis)
            analysis.partial_extra_total = keyword_amount
            analysis.partial_match_applied = True
            extra_msg = (
                f" + {kw_count} riga/e accessoria/e €{keyword_amount:.2f} "
                f"(aggiunte come POL extra)"
            )
        else:
            extra_msg = ""

        analysis.actions_suggested = [
            f"OdA-ledger: la fattura consuma {match_info['count']} riga/righe "
            f"libere su {n_libere_tot} dell'OdA {po_name} "
            f"(merci €{target:.2f} = somma righe selezionate{extra_msg}). "
            f"Righe: {match_info.get('desc','')}. "
            f"Registrare collegando alle righe selezionate."
        ]
        return True

    @staticmethod
    def _norm_desc(s: str) -> str:
        """Normalizza descrizione per confronto fuzzy: rimuove [...], lowercase,
        sostituisce non-alfanumerici con spazi singoli."""
        if not s:
            return ''
        import re as _re
        s = _re.sub(r'\[[^\]]+\]', '', s)
        s = s.lower()
        s = _re.sub(r'[^a-z0-9]+', ' ', s)
        return s.strip()

    @staticmethod
    def _only_digits(s: str) -> str:
        import re as _re
        return _re.sub(r'\D+', '', s or '')

    def _score_candidate_evidence(self, analysis: FatturaPAAnalysis,
                                  po: Dict) -> Dict[str, Any]:
        """Calcola le evidenze di un candidato OdA per la fattura.
        Ritorna dict con: cod_quota, desc_quota, commessa_match, n_inv_lines.
        Le quote sono float in [0,1] o None se non valutabile (es. fornitore
        di servizi senza righe articolo)."""
        from difflib import SequenceMatcher

        ev = {
            'cod_quota': None,
            'desc_quota': None,
            'commessa_match': False,
            'n_inv_lines': 0,
        }

        # Match commessa: post-filtro additivo (non bloccante)
        commesse = analysis.xml_data.commessa_riferimenti if analysis.xml_data else []
        if commesse:
            origin = (po.get('origin') or '').upper()
            ev['commessa_match'] = any(s.upper() in origin for s in commesse)

        # Carico righe OdA (cache)
        po_id = po.get('id')
        if not po_id:
            return ev
        if po_id in self._po_lines_cache:
            po_lines = self._po_lines_cache[po_id]
        else:
            try:
                po_lines = self.client.get_purchase_order_lines(
                    po.get('order_line', [])
                ) or []
                self._po_lines_cache[po_id] = po_lines
            except Exception:
                return ev
        if not po_lines:
            return ev

        # Estraggo codici articolo dalle righe OdA (dal name tra [...])
        import re as _re
        po_codes = set()
        po_descs_norm = []
        for l in po_lines:
            name = l.get('name') or ''
            for m in _re.findall(r'\[([^\]]+)\]', name):
                po_codes.add(self._only_digits(m))
            # Anche default_code se presente nel product
            nd = self._norm_desc(name)
            if nd:
                po_descs_norm.append(nd)
        po_codes.discard('')

        # Considero solo righe con prezzo > 0 (escludo spese accessorie EUR 0)
        inv_lines = [r for r in (analysis.xml_data.righe if analysis.xml_data else [])
                     if (r.prezzo_totale or 0) > 0]
        ev['n_inv_lines'] = len(inv_lines)
        if not inv_lines:
            return ev

        # Quota righe XML con codice presente nelle righe OdA
        inv_with_codes = [r for r in inv_lines if r.codice_articolo_valore]
        if inv_with_codes and po_codes:
            matched_cod = 0
            for r in inv_with_codes:
                c_inv = self._only_digits(r.codice_articolo_valore)
                if not c_inv:
                    continue
                if c_inv in po_codes:
                    matched_cod += 1
                    continue
                for c_po in po_codes:
                    if c_inv in c_po or c_po in c_inv:
                        matched_cod += 1
                        break
            ev['cod_quota'] = matched_cod / len(inv_with_codes)

        # Quota righe XML con descrizione simile a una riga OdA
        if po_descs_norm:
            matched_desc = 0
            valid_inv = 0
            for r in inv_lines:
                nd = self._norm_desc(r.descrizione)
                if len(nd) < 3:
                    continue
                valid_inv += 1
                best = 0.0
                for pd in po_descs_norm:
                    ratio = SequenceMatcher(None, nd, pd).ratio()
                    if ratio > best:
                        best = ratio
                if best >= 0.65:
                    matched_desc += 1
            if valid_inv:
                ev['desc_quota'] = matched_desc / valid_inv

        return ev

    def _try_implicit_match(self, analysis: FatturaPAAnalysis) -> None:
        """
        Tenta un match implicito OdA basato su fornitore + importo imponibile.
        Usato quando la fattura non cita alcun OdA nell'XML.

        Logica multi-evidenza:
        - 1 candidato per importo + conferma (cod articolo o descrizione) -> MATCH_IMPLICITO forte
        - 1 candidato per importo + nessuna conferma -> MATCH_IMPLICITO con warning ('solo importo')
        - 2+ candidati: se solo 1 ha conferma forte (cod o desc >=50%) -> MATCH_IMPLICITO su quello
        - 2+ candidati con conferme pari -> MATCH_IMPLICITO_AMBIGUO
        - Post-filtro commessa: se commessa S##### nell'XML e qualche candidato ha
          origin che la contiene, restringe il pool a quelli (additivo, non bloccante).

        Se 0 candidati: non modifica classification (il caller assegnerà NO_ODA).
        """
        if not analysis.xml_data:
            return

        inv_untaxed = float(analysis.xml_data.imponibile_totale or 0)
        if inv_untaxed <= 0:
            return

        # Ottengo partner_id della fattura. Devo dedurlo dall'anagrafica Odoo
        # cercando il fornitore tramite P.IVA (se disponibile) o dal
        # campo xml_supplier_id già presente in attachment.
        # Siccome analysis.invoice (l'attachment iniziale) aveva xml_supplier_id,
        # lo passo tramite una property che ho messo qui.
        partner_id = getattr(analysis, '_partner_id_odoo', None)
        if not partner_id:
            # Se non ce l'ho, non posso fare match implicito
            return

        # Uso cache per evitare query duplicate sullo stesso partner
        cache_key = (partner_id, round(inv_untaxed, 2))
        if cache_key in self._implicit_cache:
            candidates = self._implicit_cache[cache_key]
            loose_used = False
        else:
            try:
                # STEP 1: ricerca stretta (tolleranza al centesimo)
                candidates = self.client.search_po_by_partner_and_amount(
                    partner_id=partner_id,
                    target_untaxed=inv_untaxed,
                    tolerance_percent=self.implicit_match_tolerance_percent,
                    tolerance_absolute=self.implicit_match_tolerance_absolute,
                    states=['purchase'],
                    invoice_statuses=['to invoice'],
                )
                loose_used = False

                # STEP 2: se stretta vuota, riprovo con tolleranza larga.
                # Accetto SOLO se trovo esattamente 1 candidato univoco,
                # altrimenti lascio a 0 (niente match).
                if not candidates:
                    loose_candidates = self.client.search_po_by_partner_and_amount(
                        partner_id=partner_id,
                        target_untaxed=inv_untaxed,
                        tolerance_percent=self.implicit_match_loose_tolerance_percent,
                        tolerance_absolute=self.implicit_match_loose_tolerance_absolute,
                        states=['purchase'],
                        invoice_statuses=['to invoice'],
                    )
                    if len(loose_candidates) == 1:
                        candidates = loose_candidates
                        loose_used = True
                    # Se >1, lascio candidates vuoto (ambiguità = skip)

                self._implicit_cache[cache_key] = candidates
            except Exception as e:
                analysis.warnings.append(f"Errore ricerca match implicito: {e}")
                return

        analysis.implicit_match_candidates = candidates

        if len(candidates) == 0:
            # Nessun candidato, il caller procederà con NO_ODA
            return

        # Post-filtro commessa: se l'XML ha S##### e qualche candidato ha
        # origin che lo contiene, restringo il pool. Riduce ambiguità nei
        # casi tipo "ritiro al banco" Wuerth con OdA creato a posteriori.
        commesse = analysis.xml_data.commessa_riferimenti if analysis.xml_data else []
        if commesse and len(candidates) > 1:
            with_commessa = [c for c in candidates
                             if any(s.upper() in (c.get('origin') or '').upper()
                                    for s in commesse)]
            if with_commessa:
                candidates = with_commessa

        # Calcolo evidenze per ogni candidato
        scored = []
        for po in candidates:
            ev = self._score_candidate_evidence(analysis, po)
            ev['po'] = po
            scored.append(ev)

        def _has_strong_evidence(ev):
            """True se cod articolo o desc confermano (quota >= 50%)."""
            if ev['cod_quota'] is not None and ev['cod_quota'] >= 0.5:
                return True
            if ev['desc_quota'] is not None and ev['desc_quota'] >= 0.5:
                return True
            return False

        strong = [s for s in scored if _has_strong_evidence(s)]

        chosen = None
        evidence_label = 'amount_only'
        if len(scored) == 1:
            # Singolo candidato: passa anche con sola evidenza importo (warning)
            chosen = scored[0]
            evidence_label = 'amount+strong' if _has_strong_evidence(chosen) else 'amount_only'
        elif len(strong) == 1:
            # Più candidati per importo, ma solo uno conferma con cod/desc
            chosen = strong[0]
            evidence_label = 'amount+strong'
        elif len(strong) == 0 and len(scored) > 1:
            # Più candidati per importo, nessuno conferma con cod/desc
            # -> ambiguo (non posso scegliere)
            chosen = None
        else:
            # Più candidati con conferma: ambiguo
            chosen = None

        if chosen is None:
            analysis.classification = "MATCH_IMPLICITO_AMBIGUO"
            candidates_desc = ", ".join(
                f"{s['po']['name']} (€{s['po']['amount_untaxed']:.2f}, "
                f"{s['po'].get('date_order', '')[:10]})"
                for s in scored[:5]
            )
            analysis.actions_suggested.append(
                f"Match implicito ambiguo: trovati {len(scored)} OdA dello stesso "
                f"fornitore con imponibile ≈€{inv_untaxed:.2f}. "
                f"Candidati: {candidates_desc}"
                + ("..." if len(scored) > 5 else "")
                + ". Scelta manuale necessaria."
            )
            return

        po = chosen['po']
        analysis.purchase_order = po
        analysis.implicit_match_applied = True
        analysis.implicit_match_used_loose = loose_used
        analysis.implicit_match_evidence = evidence_label
        analysis.classification = "MATCH_IMPLICITO"
        diff = inv_untaxed - float(po['amount_untaxed'])

        # Costruisco messaggio con dettaglio evidenze
        ev_parts = ['importo']
        if chosen['commessa_match']:
            ev_parts.append('commessa')
        if chosen['cod_quota'] is not None and chosen['cod_quota'] >= 0.5:
            ev_parts.append(f"codice articolo {chosen['cod_quota']*100:.0f}%")
        if chosen['desc_quota'] is not None and chosen['desc_quota'] >= 0.5:
            ev_parts.append(f"descrizioni {chosen['desc_quota']*100:.0f}%")
        ev_summary = ' + '.join(ev_parts)

        tol_note = ""
        if loose_used:
            tol_note = (" (tolleranza larga: scostamento "
                        f"€{abs(diff):.2f}, probabile arrotondamento)")
        warn_note = ""
        if evidence_label == 'amount_only':
            warn_note = (" Conferma SOLO da importo: descrizione e codice "
                         "articolo non disponibili o non combaciano. "
                         "Verificare manualmente prima del posting.")
            analysis.warnings.append(
                f"Match implicito su {po['name']} con sola evidenza importo "
                f"(no codArt no desc). Consigliata review manuale."
            )

        analysis.actions_suggested.append(
            f"Match implicito ({ev_summary}): OdA {po['name']} con imponibile "
            f"€{po['amount_untaxed']:.2f} (fattura €{inv_untaxed:.2f}"
            + (f", diff €{diff:+.2f}" if abs(diff) >= 0.01 else "")
            + f").{tol_note}{warn_note} Registrare collegando a {po['name']}."
        )

    def _try_partial_match(self, analysis: FatturaPAAnalysis) -> None:
        """
        Match parziale: cerca un sottoinsieme di righe fattura che
        coincide esattamente con l'imponibile di un OdA del fornitore.
        Le righe escluse dal sottoinsieme sono classificate come "extra".

        Algoritmo:
        1. Recupero tutti gli OdA aperti del fornitore
        2. Per ogni OdA candidato, enumero sottoinsiemi di righe fattura
        3. Se un sottoinsieme di righe somma esattamente l'imponibile OdA ->
           MATCH_PARZIALE_OK con extra
        4. Se pi OdA matchano con sottoinsiemi diversi -> AMBIGUO

        Protezioni:
        - Max MATCH_PARZIALE_MAX_RIGHE righe (altrimenti esplosione combinatoria)
        - Singole righe extra non possono essere > MAX_EXTRA_PERCENT
        - Deve restare almeno 1 riga nel sottoinsieme matchato
        """
        if not analysis.xml_data or not analysis.xml_data.righe:
            return

        partner_id = getattr(analysis, '_partner_id_odoo', None)
        if not partner_id:
            return

        righe = analysis.xml_data.righe
        n_righe = len(righe)

        # Limite computazionale
        if n_righe > self.partial_match_max_rows:
            analysis.warnings.append(
                f"Match parziale saltato: troppe righe ({n_righe} > "
                f"{self.partial_match_max_rows})"
            )
            return

        if n_righe < 2:
            # Con 1 riga sola non ha senso parlare di "match parziale"
            # (sarebbe un match implicito puro, già tentato)
            return

        inv_untaxed = float(analysis.xml_data.imponibile_totale or 0)
        if inv_untaxed <= 0:
            return

        # Recupero OdA aperti del fornitore (con cache).
        # Filtro stretto su invoice_status='to invoice': escludo OdA legacy
        # con stato 'no' (saldati/vuoti) per evitare falsi positivi del match
        # parziale (es. MEF/IPO0007791: riga RJ45 da EUR 280 gia' fatturata
        # nel 2024 si abbinava a riga "Bobina legno" da EUR 280 del 2026).
        if partner_id in self._open_pos_cache:
            open_pos = self._open_pos_cache[partner_id]
        else:
            try:
                open_pos = self.client.get_all_open_pos_for_partner(
                    partner_id, invoice_statuses=['to invoice']
                )
                self._open_pos_cache[partner_id] = open_pos
            except Exception as e:
                analysis.warnings.append(f"Errore recupero OdA aperti: {e}")
                return

        if not open_pos:
            return

        # Enumero sottoinsiemi di righe (2^N - 1 non vuoti; escludo anche
        # il sottoinsieme "tutte le righe" che sarebbe il match pieno)
        from itertools import combinations

        # Righe come (indice, prezzo_totale) ordinate per importo decrescente
        # per scartare pi velocemente i sottoinsiemi fuori range
        line_totals = [(i, float(r.prezzo_totale)) for i, r in enumerate(righe)]

        tol = self.partial_match_tolerance_absolute

        # Per ogni OdA candidato cerco sottoinsieme matchante
        # Struttura: {oda_id: (oda_dict, sottoinsieme_indici, importo_sub)}
        matches_found = []

        for po in open_pos:
            target = float(po.get('amount_untaxed', 0))
            if target <= 0:
                continue

            # Scarto se target > totale fattura (non pu matchare un sottoinsieme
            # la cui somma supera il totale)
            if target > inv_untaxed + tol:
                continue

            # Se target ≈ totale fattura, non  match PARZIALE ma completo
            # (e sarebbe stato gi catturato dal match implicito). Skip.
            if abs(target - inv_untaxed) <= tol:
                continue

            # Cerco sottoinsiemi di dimensione crescente (1..N-1)
            found_for_this_po = None
            for size in range(1, n_righe):  # 1..N-1
                for combo in combinations(line_totals, size):
                    subtotal = sum(t for _, t in combo)
                    if abs(subtotal - target) <= tol:
                        found_for_this_po = ([i for i, _ in combo], subtotal)
                        break
                if found_for_this_po:
                    break

            if found_for_this_po:
                indices, subtotal = found_for_this_po
                matches_found.append((po, indices, subtotal))

        if not matches_found:
            return

        # Filtro righe extra: una singola riga extra non deve essere >N% dell'imponibile
        max_extra = inv_untaxed * (self.partial_match_max_extra_percent / 100.0)
        valid_matches = []
        for po, indices, subtotal in matches_found:
            indices_set = set(indices)
            extra_lines = [righe[i] for i in range(n_righe) if i not in indices_set]
            max_single_extra = max((float(r.prezzo_totale) for r in extra_lines),
                                   default=0)
            if max_single_extra > max_extra:
                # C' una riga extra troppo grande -> match sospetto
                continue
            valid_matches.append((po, indices, subtotal, extra_lines))

        if not valid_matches:
            analysis.warnings.append(
                "Match parziale scartato: righe extra eccedono soglia sicurezza."
            )
            return

        if len(valid_matches) == 1:
            po, indices, subtotal, extra_lines = valid_matches[0]
            analysis.purchase_order = po
            analysis.partial_match_applied = True
            analysis.partial_extra_lines = [
                {
                    'descrizione': r.descrizione,
                    'quantita': r.quantita,
                    'prezzo': r.prezzo_totale,        # legacy, retrocompat
                    'prezzo_totale': r.prezzo_totale, # consumato dal writer
                    'prezzo_unitario': r.prezzo_unitario,
                    'aliquota_iva': r.aliquota_iva,
                    'unita_misura': r.unita_misura,
                    'codice_articolo_valore': getattr(r, 'codice_articolo_valore', ''),
                } for r in extra_lines
            ]
            analysis.partial_extra_total = sum(float(r.prezzo_totale)
                                               for r in extra_lines)
            analysis.classification = "MATCH_PARZIALE_OK"

            extra_desc = "; ".join(
                f"{(r.descrizione or '')[:40]} (€{float(r.prezzo_totale):.2f})"
                for r in extra_lines[:3]
            )
            if len(extra_lines) > 3:
                extra_desc += f"; ... (+{len(extra_lines)-3} altre)"

            analysis.actions_suggested.append(
                f"Match parziale: OdA {po['name']} (€{float(po['amount_untaxed']):.2f}) "
                f"matcha {len(indices)} su {n_righe} righe fattura. "
                f"Righe extra non nell'OdA: {len(extra_lines)} per un totale di "
                f"€{analysis.partial_extra_total:.2f} ({extra_desc}). "
                f"VERIFICARE che le righe extra siano da registrare comunque "
                f"(spese, consegne aggiuntive) prima di confermare."
            )
            return

        # Pi OdA candidati con sottoinsiemi diversi -> AMBIGUO
        analysis.classification = "MATCH_PARZIALE_AMBIGUO"
        cand_desc = ", ".join(
            f"{po['name']} (€{float(po['amount_untaxed']):.2f})"
            for po, _, _, _ in valid_matches[:5]
        )
        analysis.actions_suggested.append(
            f"Match parziale ambiguo: {len(valid_matches)} OdA del fornitore "
            f"matchano sottoinsiemi diversi delle righe fattura. "
            f"Candidati: {cand_desc}"
            + ("..." if len(valid_matches) > 5 else "")
            + ". Scelta manuale necessaria."
        )

    def _try_oda_line_subset_suggestions(self, analysis: FatturaPAAnalysis) -> None:
        """
        Cerca OdA aperti del fornitore dove un sottoinsieme di RIGHE OdA
        (non righe fattura!) somma esattamente l'imponibile della fattura.

        Usato per casi come:
        - Canoni locazione mensili: OdA ha 12 righe da €5000, fattura è €5000
          -> una riga OdA matcha l'imponibile -> SUGGESTION
        - Servizi a blocchi: OdA ha gruppo ricorrente "€4500 + €625 + €205"
          che somma €5330, fattura è €5330 -> SUGGESTION

        NON classifica come registrabile (rischio falsi positivi). Classifica
        come NO_ODA_CON_SUGGERIMENTI con lista OdA candidati per la
        registrazione manuale guidata.

        Se non trova nessun match valido, non tocca classification (caller
        metterà NO_ODA_DA_CLASSIFICARE).
        """
        if not analysis.xml_data:
            return

        inv_untaxed = float(analysis.xml_data.imponibile_totale or 0)
        if inv_untaxed <= 0:
            return

        partner_id = getattr(analysis, '_partner_id_odoo', None)
        if not partner_id:
            return

        # Recupero OdA aperti del fornitore (limitati agli ultimi N mesi).
        # Filtro stretto su invoice_status='to invoice' (esclusi i 'no' legacy).
        if partner_id in self._open_pos_cache_recent:
            open_pos = self._open_pos_cache_recent[partner_id]
        else:
            try:
                open_pos = self.client.get_all_open_pos_for_partner(
                    partner_id,
                    invoice_statuses=['to invoice'],
                    max_age_months=self.suggestions_max_age_months,
                )
                self._open_pos_cache_recent[partner_id] = open_pos
            except Exception as e:
                analysis.warnings.append(f"Errore recupero OdA per suggerimenti: {e}")
                return

        if not open_pos:
            return

        tol = self.suggestions_tolerance_absolute
        # Tolleranza secondaria calibrata: min(€50, max(€5, 2% imp))
        # Pensata per assorbire spese accessorie (trasporto, contributi)
        # senza ammettere falsi positivi su importi piccoli.
        sec_tol = min(50.0, max(5.0, 0.02 * inv_untaxed))
        suggestions = []

        for po in open_pos:
            po_id = po.get('id')
            if not po_id:
                continue

            # Recupero righe OdA (con cache)
            if po_id in self._po_lines_cache:
                lines = self._po_lines_cache[po_id]
            else:
                try:
                    lines = self.client.get_purchase_order_lines(
                        po.get('order_line', [])
                    ) or []
                    self._po_lines_cache[po_id] = lines
                except Exception:
                    continue

            if not lines or len(lines) > self.suggestions_max_lines:
                continue

            # Cerco sottoinsieme che matcha inv_untaxed (1° pass stretto,
            # 2° pass con tolleranza secondaria + extra_amount)
            match_info = self._find_subset_match(lines, inv_untaxed, tol,
                                                 secondary_tolerance=sec_tol)
            if match_info:
                suggestions.append({
                    'po_id': po_id,
                    'po_name': po.get('name'),
                    'po_untaxed': float(po.get('amount_untaxed', 0)),
                    'match_line_count': match_info['count'],
                    'match_type': match_info['type'],
                    'match_lines_desc': match_info['desc'],
                    'match_line_ids': match_info.get('line_ids', []),
                    'extra_amount': match_info.get('extra_amount', 0.0),
                })

        if not suggestions:
            return

        analysis.suggested_pos = suggestions

        if len(suggestions) == 1:
            # Suggerimento UNIVOCO -> promuovo a MATCH_DA_SUGGERIMENTO o
            # MATCH_DA_SUGGERIMENTO_PIU_EXTRA in base alla presenza di un
            # extra_amount (delta positivo = spese accessorie da aggiungere).
            s = suggestions[0]
            sugg_po = next((p for p in open_pos if p.get('id') == s['po_id']), None)
            extra = float(s.get('extra_amount') or 0)
            if sugg_po:
                analysis.purchase_order = sugg_po
                if abs(extra) <= tol:
                    # Match al centesimo: classification standard
                    analysis.classification = "MATCH_DA_SUGGERIMENTO"
                    analysis.actions_suggested.append(
                        f"Match da suggerimento univoco: OdA {s['po_name']} ha "
                        f"{s['match_line_count']} righe che sommano l'imponibile "
                        f"fattura (€{inv_untaxed:.2f}). OdA totale "
                        f"€{s['po_untaxed']:.2f}, righe matchanti: "
                        f"{s['match_lines_desc']}. "
                        f"Verificare manualmente prima del posting."
                    )
                else:
                    # Match con extra (spese accessorie tipo trasporto)
                    analysis.classification = "MATCH_DA_SUGGERIMENTO_PIU_EXTRA"
                    sign = "+" if extra > 0 else "-"
                    analysis.actions_suggested.append(
                        f"Match da suggerimento univoco con extra: OdA "
                        f"{s['po_name']} ha {s['match_line_count']} righe per "
                        f"€{inv_untaxed - extra:.2f}, fattura €{inv_untaxed:.2f} "
                        f"(extra {sign}€{abs(extra):.2f}, probabili spese "
                        f"accessorie). Verificare manualmente prima del posting."
                    )
            else:
                # Fallback: PO non recuperabile (improbabile), resto in NO_ODA
                analysis.classification = "NO_ODA_CON_SUGGERIMENTI"
                analysis.actions_suggested.append(
                    f"Suggerimento: OdA {s['po_name']} del fornitore ha "
                    f"{s['match_line_count']} righe che sommano l'imponibile fattura "
                    f"(€{inv_untaxed:.2f}). OdA totale €{s['po_untaxed']:.2f}, "
                    f"righe matchanti: {s['match_lines_desc']}. "
                    f"Verificare e registrare manualmente collegando a {s['po_name']}."
                )
        else:
            analysis.classification = "NO_ODA_CON_SUGGERIMENTI"
            names = ", ".join(s['po_name'] for s in suggestions[:5])
            analysis.actions_suggested.append(
                f"Suggerimenti: {len(suggestions)} OdA del fornitore hanno "
                f"righe matchanti l'imponibile fattura (€{inv_untaxed:.2f}): "
                f"{names}"
                + ("..." if len(suggestions) > 5 else "")
                + ". Verificare manualmente quale usare."
            )

    def _find_subset_match(self, lines: List[Dict], target: float,
                           tolerance: float,
                           secondary_tolerance: float = 0.0) -> Optional[Dict]:
        """
        Cerca un sottoinsieme di righe la cui somma = target.

        - tolerance: tolleranza primaria (stretta, default €0.01).
          Match al centesimo: classifica MATCH_DA_SUGGERIMENTO.
        - secondary_tolerance: tolleranza secondaria (più larga). Quando il
          match scatta solo con questa, ritorna con `extra_amount` = delta
          (positivo = fattura supera subset OdA, di solito spese accessorie
          come trasporto). Classifica MATCH_DA_SUGGERIMENTO_PIU_EXTRA.

        IMPORTANTE — Filtro righe già fatturate:
        Le righe con qty_invoiced >= product_qty NON sono disponibili
        (già consumate da fatture precedenti). Per le righe parzialmente
        fatturate uso un subtotal residuo proporzionale al qty rimanente.

        Ritorna dict {count, type, desc, line_ids, extra_amount} se trovato.
        extra_amount = 0 per match al centesimo, > 0 per match con extra.
        """
        from itertools import combinations

        if not lines:
            return None

        # Estraggo (po_line_id, subtotal_residuo, descrizione)
        # Filtro righe già fatturate al 100%; per parziali uso il residuo.
        items = []
        for i, l in enumerate(lines):
            subtotal = float(l.get('price_subtotal', 0) or 0)
            if subtotal <= 0:
                continue
            qty = float(l.get('product_qty') or 0)
            qty_inv = float(l.get('qty_invoiced') or 0)
            if qty <= 0:
                continue
            qty_residua = qty - qty_inv
            if qty_residua <= 0.001:
                # Riga completamente fatturata: non disponibile
                continue
            # Per righe parziali: subtotal residuo proporzionale
            if qty_inv > 0:
                subtotal_residuo = subtotal * (qty_residua / qty)
            else:
                subtotal_residuo = subtotal
            if subtotal_residuo <= 0:
                continue
            prod = l.get('product_id')
            prod_name = prod[1] if isinstance(prod, list) else ''
            desc = l.get('name') or prod_name or f"riga {i+1}"
            line_id = l.get('id')
            items.append((line_id, round(subtotal_residuo, 2), desc[:40]))

        if not items:
            return None

        def _build_match(combo, count, type_name, desc_format, extra=0.0):
            total_combo = sum(a for _, a, _ in combo)
            if count == 1:
                line_id, amount, desc = combo[0]
                ds = f"1 riga {desc} (€{amount:.2f})"
            elif count <= 3:
                ds = " + ".join(f"{d} (€{a:.2f})" for _, a, d in combo)
                ds = f"{count} righe: {ds}"
            elif count == 4:
                ds = " + ".join(f"€{a:.2f}" for _, a, _ in combo)
                ds = f"{count} righe: {ds}"
            else:
                # Per >4 righe (es. fattura conclusiva): testo riassuntivo
                ds = f"{count} righe libere dell'OdA (totale €{total_combo:.2f})"
            if extra:
                ds += f" + EXTRA €{extra:.2f}"
            return {
                'count': count,
                'type': type_name,
                'desc': ds,
                'line_ids': [c[0] for c in combo],
                'extra_amount': round(extra, 2),
            }

        # PRIMA PASSATA: tolleranza stretta (al centesimo)
        for k in range(1, 5):
            if k > len(items):
                break
            if k == 4 and len(items) > 40:
                break
            type_name = ['', 'single_line', 'pair', 'triple', 'quadruple'][k]
            for combo in combinations(items, k):
                s = sum(a for _, a, _ in combo)
                if abs(s - target) <= tolerance:
                    return _build_match(combo, k, type_name, None, extra=0.0)

        # PRIMA PASSATA — FASE 4b: ricerca per COMPLEMENTO.
        # Se target e' vicino al totale items, cercare un subset di righe da
        # ESCLUDERE e' equivalente a cercare il subset da includere ma con
        # combinatoria molto piu' bassa. Sblocca casi tipo CONRAD: 8 POL
        # totali, fattura consuma 6/8, complemento = 2 POL non consumate.
        total_sum_items = sum(a for _, a, _ in items)
        remainder = total_sum_items - target
        if (remainder > tolerance
                and len(items) >= 6
                and len(items) <= 40):
            # Cerco un subset di righe la cui somma = remainder (= righe non
            # consumate dalla fattura). k massimo: meta' items, max 4.
            max_k = min(4, len(items) // 2)
            for k_excl in range(1, max_k + 1):
                for excluded in combinations(items, k_excl):
                    s_excl = sum(a for _, a, _ in excluded)
                    if abs(s_excl - remainder) <= tolerance:
                        excluded_ids = {c[0] for c in excluded}
                        included = [it for it in items if it[0] not in excluded_ids]
                        if included:
                            return _build_match(
                                included, len(included),
                                f'complement_excl_{k_excl}', None, extra=0.0
                            )

        # PRIMA PASSATA — FASE 5: somma TOTALE di tutte le righe libere
        # (caso "fattura conclusiva" che chiude tutte le righe libere dell'OdA)
        if len(items) > 4:
            total_sum = sum(a for _, a, _ in items)
            if abs(total_sum - target) <= tolerance:
                return _build_match(items, len(items), 'all_lines', None,
                                    extra=0.0)

        # SECONDA PASSATA: tolleranza secondaria (con extra)
        if secondary_tolerance > tolerance:
            best = None
            best_diff = None
            for k in range(1, 5):
                if k > len(items):
                    break
                if k == 4 and len(items) > 40:
                    break
                type_name = ['', 'single_line', 'pair', 'triple', 'quadruple'][k]
                for combo in combinations(items, k):
                    s = sum(a for _, a, _ in combo)
                    diff = target - s
                    abs_diff = abs(diff)
                    if abs_diff <= secondary_tolerance:
                        if best is None or abs_diff < best_diff:
                            best = (combo, k, type_name, diff)
                            best_diff = abs_diff
            # Considero anche la somma totale come candidato
            if len(items) > 4:
                total_sum = sum(a for _, a, _ in items)
                diff = target - total_sum
                abs_diff = abs(diff)
                if abs_diff <= secondary_tolerance:
                    if best is None or abs_diff < best_diff:
                        best = (items, len(items), 'all_lines', diff)
                        best_diff = abs_diff
            if best:
                combo, k, type_name, diff = best
                return _build_match(combo, k, type_name, None, extra=diff)

        return None

    def _try_supplier_fixed_mapping(self, analysis: FatturaPAAnalysis) -> None:
        """
        Mappatura fornitori fissi: per fornitori con pattern certo
        (Trenitalia, Italo), applica OdA + conto contabile fissi.

        Condizioni per applicare la mappatura:
        1. Fornitore identificato via P.IVA (xml_data.p_iva_fornitore)
        2. P.IVA presente nella supplier_mapping di config
        3. L'OdA fisso esiste in Odoo (cache _po_cache)
        4. L'OdA è in stato 'purchase' (non draft, non chiuso)

        Se tutte le condizioni sono soddisfatte, classifica come
        MAPPATURA_FORNITORE_FISSO (verde, registrabile manualmente su
        OdA + conto specificati).
        """
        if not analysis.xml_data:
            return

        supplier_vat = (analysis.xml_data.cedente_partita_iva or '').strip().upper()
        if not supplier_vat:
            return

        # === Check Automezzi (categoria distinta) ===
        # Se la P.IVA è di un fornitore noleggio veicoli del file Parco Auto,
        # classifica come MAPPATURA_AUTOMEZZI e salta il flusso fornitore fisso.
        try:
            from config.rules import (AUTOMEZZI_VATS, MAPPATURA_AUTOMEZZI,
                                        MAPPATURA_AUTOMEZZI_ATTIVA)
            if MAPPATURA_AUTOMEZZI_ATTIVA and supplier_vat in AUTOMEZZI_VATS:
                automezzi_entry = MAPPATURA_AUTOMEZZI[supplier_vat]
                nome = automezzi_entry.get('nome', supplier_vat)
                analysis.classification = "MAPPATURA_AUTOMEZZI"
                analysis.actions_suggested.append(
                    f"Mappatura automezzi: {nome}. Categoria 'Automezzi', "
                    f"writer dedicato consume-POL multi-line."
                )
                return
        except ImportError:
            # Modulo non ancora disponibile (retrocompat)
            pass

        # Normalizzo: la chiave può avere prefisso IT o no
        mapping_entry = self.supplier_mapping.get(supplier_vat)
        if not mapping_entry:
            # Prova senza prefisso IT
            if supplier_vat.startswith('IT'):
                mapping_entry = self.supplier_mapping.get(supplier_vat[2:])
            else:
                mapping_entry = self.supplier_mapping.get('IT' + supplier_vat)

        if not mapping_entry:
            return

        # Risolvi multi-contratto (es. Telecom: P.IVA + contratto → OdA)
        from config.rules import resolve_mapping_entry
        resolved = resolve_mapping_entry(mapping_entry, analysis.xml_data)
        if not resolved:
            # Multi-contratto ma nessun contratto matchato → skip silenzioso
            if mapping_entry.get('multi_contratto'):
                return
            resolved = mapping_entry

        oda_name = resolved.get('oda_fisso')
        conto = resolved.get('conto_contabile')
        nome = resolved.get('nome', supplier_vat)

        if not oda_name:
            return

        # Verifico che l'OdA esista e sia aperto
        try:
            po = self.client.search_purchase_order_by_name(oda_name)
        except Exception as e:
            analysis.warnings.append(
                f"Errore verifica OdA fisso {oda_name}: {e}"
            )
            return

        if not po:
            # OdA fisso non esiste più: avviso ma non applico
            analysis.warnings.append(
                f"Mappatura fornitore {nome}: OdA fisso {oda_name} non "
                f"trovato in Odoo. Verificare/aggiornare config."
            )
            return

        po_state = po.get('state', '')
        if po_state != 'purchase':
            # OdA non attivo: segnalo ma non applico automaticamente
            analysis.warnings.append(
                f"Mappatura fornitore {nome}: OdA fisso {oda_name} in stato "
                f"'{po_state}' (non attivo). Verificare/creare nuovo OdA."
            )
            return

        # Se arrivo qui, la mappatura è valida. Applico.
        analysis.purchase_order = po
        analysis.classification = "MAPPATURA_FORNITORE_FISSO"
        analysis.actions_suggested.append(
            f"Mappatura fornitore fisso: {nome} registra sempre su "
            f"OdA {oda_name} (imponibile OdA €{po.get('amount_untaxed', 0):.2f}) "
            f"e conto contabile {conto} ({resolved.get('conto_descrizione', '')}). "
            f"Registrare manualmente collegando a {oda_name}."
        )

    def apply_duplicate_guard(self, analyses: List[FatturaPAAnalysis]) -> None:
        """
        Post-processing su tutte le analisi della run.
        Se 2+ fatture MATCH_IMPLICITO puntano allo stesso OdA, le declassa
        TUTTE a MATCH_IMPLICITO_AMBIGUO per forzare la scelta manuale.

        Questo previene casi come il doppio HILTI dove due fatture dello
        stesso fornitore con importi simili sono state entrambe matchate
        allo stesso OdA, ma in realtà solo una era quella corretta.
        """
        if not self.implicit_match_duplicate_guard:
            return

        # Raggruppo le analisi MATCH_IMPLICITO per OdA assegnato
        po_to_analyses: Dict[str, List[FatturaPAAnalysis]] = {}
        for a in analyses:
            if a.classification != "MATCH_IMPLICITO":
                continue
            if not a.purchase_order:
                continue
            po_name = a.purchase_order.get('name', '')
            if not po_name:
                continue
            po_to_analyses.setdefault(po_name, []).append(a)

        # Per ogni OdA con 2+ fatture, declassa tutte ad AMBIGUO
        for po_name, group in po_to_analyses.items():
            if len(group) < 2:
                continue

            # Costruisco il sommario dei "gemelli"
            peers_desc = ", ".join(
                f"{a.invoice_number} (€{float(a.xml_data.imponibile_totale or 0):.2f})"
                for a in group
            )

            for a in group:
                a.classification = "MATCH_IMPLICITO_AMBIGUO"
                # Rimuovo il messaggio precedente e sostituisco
                a.actions_suggested = [
                    f"Match implicito AMBIGUO per guardia duplicati: "
                    f"{len(group)} fatture dello stesso fornitore puntano tutte "
                    f"all'OdA {po_name} (candidati: {peers_desc}). "
                    f"Solo una dovrebbe essere collegata all'OdA, le altre sono "
                    f"probabilmente per ordini diversi. Scelta manuale necessaria."
                ]
                a.warnings.append(
                    f"Duplicate guard attivato: OdA {po_name} aveva "
                    f"{len(group)} candidati in questa esecuzione."
                )

    def apply_strict_wins_over_loose(self, analyses: List[FatturaPAAnalysis]) -> None:
        """
        Post-processing: se nella stessa run una fattura ha trovato match
        STRETTO (al centesimo) su un OdA, un'altra fattura dello stesso
        fornitore che punta al medesimo OdA via tolleranza LARGA deve
        essere declassata a NO_ODA (è verosimilmente un falso positivo).

        Esempio HILTI: 
        - Fattura A €310,58 -> match stretto su P04677 (corretto)
        - Fattura B €310,00 -> match largo (tolleranza) su P04677 (FALSO!)
        La guardia declassa B a NO_ODA.
        """
        # Identifico gli OdA "prenotati" da match stretto
        strict_claimed_pos = set()
        for a in analyses:
            if a.classification != "MATCH_IMPLICITO":
                continue
            if a.implicit_match_used_loose:
                continue  # Questo è un match largo, non prenota
            if not a.purchase_order:
                continue
            po_name = a.purchase_order.get('name', '')
            if po_name:
                strict_claimed_pos.add(po_name)

        # Declasso i match larghi che puntano a OdA già prenotati
        for a in analyses:
            if a.classification != "MATCH_IMPLICITO":
                continue
            if not a.implicit_match_used_loose:
                continue
            if not a.purchase_order:
                continue
            po_name = a.purchase_order.get('name', '')
            if po_name in strict_claimed_pos:
                # Declasso: l'OdA è stato prenotato da un match stretto,
                # questa fattura con match largo è verosimilmente sbagliata
                a.classification = "NO_ODA_DA_CLASSIFICARE"
                a.purchase_order = None
                a.implicit_match_applied = False
                a.implicit_match_used_loose = False
                a.actions_suggested = [
                    f"Match implicito largo annullato: l'OdA a cui puntava "
                    f"era già stato prenotato da altra fattura con match "
                    f"esatto al centesimo. Questa fattura probabilmente non "
                    f"appartiene a quell'OdA. Classificata come NO_ODA per "
                    f"gestione in Fase 3 (fornitori fissi)."
                ]
                a.warnings.append(
                    f"Strict-wins guard: match largo su OdA {po_name} scartato "
                    f"perché già assegnato a match stretto."
                )

    def apply_run_cumulative_check(self, analyses: List[FatturaPAAnalysis]) -> None:
        """
        Post-processing su tutte le analisi della run.
        Rileva il caso in cui 2+ fatture puntano al medesimo OdA esplicito
        (indipendentemente dalla loro classificazione individuale): se la
        somma delle fatture della run + l'already_invoiced precedente
        eccede significativamente l'OdA -> CUMULATIVO_ECCEDE per tutte
        quelle che erano state classificate come registrabili.

        Esempio reale: 3 fatture Wuerth Elektronik sullo stesso OdA P04368
        (€54,14). La prima €44,22 passa come AUTO_VALIDABILE (dentro tolleranza),
        le altre due €17,98 e €8,70 vanno DA_VERIFICARE. Sommate fanno €70,90
        = +31% sopra OdA. In questo caso la AUTO_VALIDABILE va "ritirata"
        perch con le altre nel backlog il cumulato sfora.
        """
        # Raggruppo TUTTE le fatture con OdA esplicito assegnato,
        # indipendentemente dalla classificazione
        po_to_analyses: Dict[str, List[FatturaPAAnalysis]] = {}
        for a in analyses:
            if not a.purchase_order:
                continue
            po_name = a.purchase_order.get('name', '')
            if not po_name:
                continue
            po_to_analyses.setdefault(po_name, []).append(a)

        REGISTRABILI = {
            'AUTO_VALIDABILE',
            'MATCH_IMPLICITO',
            'MATCH_PARZIALE_OK',
            'PARZIALE_CUMULATIVO_OK',
        }

        for po_name, group in po_to_analyses.items():
            if len(group) < 2:
                continue

            # Prendo l'OdA di riferimento dal primo (gli altri avranno lo stesso)
            po = group[0].purchase_order
            po_untaxed = float(po.get('amount_untaxed', 0) or 0)
            if po_untaxed <= 0:
                continue

            # Calcolo la somma imponibile delle fatture della run
            run_total = 0.0
            for a in group:
                if a.xml_data:
                    run_total += float(a.xml_data.imponibile_totale or 0)

            # Prendo already_invoiced (altre fatture pregresse) dal primo
            # dell'analyses gi calcolato dalla logica cumulativa regolare
            already_invoiced = max(
                (a.cumulative_other_invoices or 0.0) for a in group
            )

            grand_total = run_total + already_invoiced
            diff = grand_total - po_untaxed
            # Considero "eccede" se supera ENTRAMBE le tolleranze (assoluta + %)
            # Usiamo la stessa tolleranza percentuale del matcher regolare
            diff_percent = (diff / po_untaxed * 100) if po_untaxed > 0 else 0
            if diff <= self.tol_total and diff_percent <= self.matcher.tol_percent:
                continue

            # P4: prima di marcare CUMULATIVO_ECCEDE, verifico se l'eccesso e'
            # spiegato dalle righe accessorie (keyword TRASPORTO/ONERI_BANCARI/
            # BOLLO) delle fatture del gruppo. Se si' -> declasso TUTTE a
            # PARZIALE_CUMULATIVO_OK e popolo partial_extra_lines per ognuna,
            # cosi' al momento della bozza il writer creera' le POL extra
            # accessorie sull'OdA (pattern Sonepar/Coel).
            extras_cumul = 0.0
            per_invoice_extras: Dict[int, float] = {}
            for a in group:
                a_kw = sum(
                    float(lm.invoice_line.get('price_subtotal', 0))
                    for lm in a.line_matches if lm.match_type == "KEYWORD"
                )
                per_invoice_extras[id(a)] = a_kw
                extras_cumul += a_kw
            diff_netto = diff - extras_cumul
            diff_netto_percent = (diff_netto / po_untaxed * 100) if po_untaxed > 0 else 0

            if (extras_cumul > 0
                    and diff_netto <= self.tol_total
                    and diff_netto_percent <= self.matcher.tol_percent):
                # Eccesso interamente spiegato dalle accessorie.
                for a in group:
                    a_kw = per_invoice_extras.get(id(a), 0.0)
                    a_kw_count = sum(
                        1 for lm in a.line_matches if lm.match_type == "KEYWORD"
                    )
                    if a_kw > 0 and a_kw_count > 0:
                        a.partial_extra_lines = (
                            self._build_extra_lines_from_keyword_matches(a)
                        )
                        a.partial_extra_total = a_kw
                        a.partial_match_applied = True
                    a.classification = "PARZIALE_CUMULATIVO_OK"
                    a.actions_suggested = [
                        f"OdA-ledger cumulativo: {len(group)} fatture della run "
                        f"consumano l'OdA {po_name} (€{po_untaxed:.2f}). "
                        f"Merci cumulate €{grand_total - extras_cumul:.2f}, "
                        f"accessorie €{extras_cumul:.2f} (verranno aggiunte come "
                        f"POL extra). Registrare."
                    ]
                continue

            # SFORA: avviso tutte le fatture del gruppo
            peers_desc = ", ".join(
                f"{a.invoice_number} (€{float(a.xml_data.imponibile_totale or 0):.2f})"
                for a in group
            )

            # Declassifico a CUMULATIVO_ECCEDE SOLO quelle che erano
            # classificate come registrabili. Le DA_VERIFICARE restano tali
            # (erano gi fuori tolleranza comunque)
            for a in group:
                if a.classification in REGISTRABILI:
                    a.classification = "CUMULATIVO_ECCEDE"
                    a.actions_suggested = [
                        f"ATTENZIONE cumulativo di run: {len(group)} fatture della "
                        f"stessa esecuzione puntano a OdA {po_name} (€{po_untaxed:.2f}). "
                        f"Somma fatture run: €{run_total:.2f}"
                        + (f", pregresso: €{already_invoiced:.2f}" if already_invoiced > 0 else "")
                        + f". Cumulato totale €{grand_total:.2f} eccede OdA di "
                        f"€{diff:.2f} ({diff/po_untaxed*100:.1f}%). "
                        f"Fatture del gruppo: {peers_desc}. "
                        f"Verificare se una  duplicata/errata o se l'OdA va integrato."
                    ]
                    a.warnings.append(
                        f"Run cumulative check: OdA {po_name} saturato da "
                        f"{len(group)} fatture ({diff/po_untaxed*100:.1f}% eccesso)."
                    )
                else:
                    # Per le altre (DA_VERIFICARE, ecc.) aggiungo solo un warning
                    a.warnings.append(
                        f"Attenzione: OdA {po_name} ha {len(group)} fatture in "
                        f"questa run per totale €{grand_total:.2f} (OdA €{po_untaxed:.2f}, "
                        f"eccesso €{diff:.2f})."
                    )
