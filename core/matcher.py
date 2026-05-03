"""
Logica di matching tra righe fattura e righe ordine di acquisto.
Confronti su: prodotto, quantità, prezzo unitario, totale riga.
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class LineMatch:
    """Risultato del match di una singola riga fattura."""
    invoice_line: Dict[str, Any]
    po_line: Optional[Dict[str, Any]] = None
    match_type: str = "NO_MATCH"        # EXACT | TOLERANCE | KEYWORD | NO_MATCH
    diff_quantity: float = 0.0
    diff_price_unit: float = 0.0
    diff_subtotal: float = 0.0
    diff_percent: float = 0.0
    notes: List[str] = field(default_factory=list)
    keyword_category: Optional[str] = None   # se riga extra riconosciuta
    keyword_account: Optional[str] = None


@dataclass
class InvoiceAnalysis:
    """Analisi completa di una fattura."""
    invoice: Dict[str, Any]
    purchase_order: Optional[Dict[str, Any]] = None
    line_matches: List[LineMatch] = field(default_factory=list)
    total_diff: float = 0.0
    total_diff_percent: float = 0.0
    classification: str = "ANOMALIA"
    priority_score: float = 0.0
    actions_suggested: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class InvoiceMatcher:
    """Esegue il matching fattura <-> OdA con tolleranze configurabili."""

    def __init__(self, tol_percent: float, tol_absolute: float,
                 tol_total: float, keyword_classifier):
        self.tol_percent = tol_percent
        self.tol_absolute = tol_absolute
        self.tol_total = tol_total
        self.keyword_classifier = keyword_classifier

    def match_line(self, inv_line: Dict, po_lines: List[Dict]) -> LineMatch:
        """
        Matcha una singola riga fattura contro le righe dell'OdA.
        Strategia: prima cerca collegamento diretto via purchase_line_id,
        poi per product_id, infine per descrizione simile.
        """
        match = LineMatch(invoice_line=inv_line)

        # 1. Match diretto se Odoo ha già collegato la riga
        po_line_link = inv_line.get('purchase_line_id')
        if po_line_link:
            po_line_id = po_line_link[0] if isinstance(po_line_link, list) else po_line_link
            for pol in po_lines:
                if pol['id'] == po_line_id:
                    return self._compare_lines(inv_line, pol, "LINKED")

        # 2. Match per prodotto
        inv_product = inv_line.get('product_id')
        if inv_product:
            inv_product_id = inv_product[0] if isinstance(inv_product, list) else inv_product
            for pol in po_lines:
                pol_product = pol.get('product_id')
                if pol_product:
                    pol_product_id = pol_product[0] if isinstance(pol_product, list) else pol_product
                    if pol_product_id == inv_product_id:
                        return self._compare_lines(inv_line, pol, "PRODUCT")

        # 3. Prova classificazione keyword (trasporto, spese, etc.)
        description = inv_line.get('name', '')
        keyword_result = self.keyword_classifier(description)
        if keyword_result:
            conto_key, conto_codice, categoria = keyword_result
            match.match_type = "KEYWORD"
            match.keyword_category = categoria
            match.keyword_account = conto_codice
            match.notes.append(
                f"Riga riconosciuta come {categoria} -> conto {conto_codice}"
            )
            return match

        # 4. Nessun match
        match.notes.append(f"Nessun match trovato per: '{description[:60]}'")
        return match

    def _compare_lines(self, inv_line: Dict, po_line: Dict,
                       match_source: str) -> LineMatch:
        """Confronta due righe matchate e calcola differenze."""
        match = LineMatch(invoice_line=inv_line, po_line=po_line)

        inv_qty = float(inv_line.get('quantity', 0))
        po_qty = float(po_line.get('product_qty', 0))
        inv_price = float(inv_line.get('price_unit', 0))
        po_price = float(po_line.get('price_unit', 0))
        inv_subtotal = float(inv_line.get('price_subtotal', 0))
        po_subtotal = float(po_line.get('price_subtotal', 0))

        match.diff_quantity = inv_qty - po_qty
        match.diff_price_unit = inv_price - po_price
        match.diff_subtotal = inv_subtotal - po_subtotal

        if po_subtotal:
            match.diff_percent = abs(match.diff_subtotal / po_subtotal * 100)

        # Classificazione del match
        within_absolute = abs(match.diff_subtotal) <= self.tol_absolute
        within_percent = match.diff_percent <= self.tol_percent

        if abs(match.diff_subtotal) < 0.01:
            match.match_type = "EXACT"
            match.notes.append(f"Match esatto ({match_source})")
        elif within_absolute or within_percent:
            match.match_type = "TOLERANCE"
            match.notes.append(
                f"Match entro tolleranza ({match_source}): "
                f"diff €{match.diff_subtotal:.2f} ({match.diff_percent:.1f}%)"
            )
        else:
            match.match_type = "OUT_OF_TOLERANCE"
            match.notes.append(
                f"Scostamento oltre tolleranza: "
                f"diff €{match.diff_subtotal:.2f} ({match.diff_percent:.1f}%)"
            )
            if abs(match.diff_quantity) > 0.01:
                match.notes.append(
                    f"Quantità: fattura {inv_qty} vs OdA {po_qty}"
                )
            if abs(match.diff_price_unit) > 0.01:
                match.notes.append(
                    f"Prezzo unitario: fattura €{inv_price:.4f} vs OdA €{po_price:.4f}"
                )

        return match
