"""Test offline con dati reali di Electro Rent, Arrow ECS, Smart IT."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.matcher import InvoiceMatcher
from core.keyword_rules import classify_line_by_keyword
from core.fatturapa_analyzer import FatturaPAAnalyzer, FatturaPAAnalysis
from core.fatturapa_parser import FatturaPAData, FatturaPALine


class FakeClient:
    def get_purchase_order_lines(self, ids):
        return []
    def search_purchase_order_by_name(self, name):
        return None

matcher = InvoiceMatcher(tol_percent=2.0, tol_absolute=5.0, tol_total=10.0,
                        keyword_classifier=classify_line_by_keyword)
analyzer = FatturaPAAnalyzer(FakeClient(), matcher, 10.0)

# Caso Arrow ECS - match perfetto
a1 = FatturaPAAnalysis(attachment_id=1)
a1.supplier_name = "ARROW ECS SRL"
a1.invoice_total = 395280.0
a1.oda_references_in_xml = ["P04532"]
a1.xml_data = FatturaPAData(
    imponibile_totale=324000.0, imposta_totale=71280.0, importo_totale=395280.0,
    righe=[FatturaPALine(numero_linea=1, descrizione="Citrix", quantita=1200,
                         prezzo_unitario=270, prezzo_totale=324000)]
)
a1.purchase_order = {'id': 1, 'name': 'P04532', 'amount_untaxed': 324000.0,
                     'amount_total': 395280.0, 'state': 'purchase', 'invoice_status': 'to invoice'}
a1.po_lines = []
# Simulo match righe
from core.matcher import LineMatch
a1.line_matches = [matcher.match_line(analyzer._line_to_invoice_dict(l), []) for l in a1.xml_data.righe]

# Caso Smart IT - match perfetto
a2 = FatturaPAAnalysis(attachment_id=2)
a2.supplier_name = "Smart IT"
a2.invoice_total = 12200.0
a2.oda_references_in_xml = ["P04663"]
a2.xml_data = FatturaPAData(
    imponibile_totale=10000.0, imposta_totale=2200.0, importo_totale=12200.0,
    righe=[FatturaPALine(numero_linea=1, descrizione="Servizi",
                         quantita=1, prezzo_unitario=10000, prezzo_totale=10000)]
)
a2.purchase_order = {'id': 2, 'name': 'P04663', 'amount_untaxed': 10000.0,
                     'amount_total': 12200.0, 'state': 'purchase', 'invoice_status': 'to invoice'}
a2.po_lines = []
a2.line_matches = [matcher.match_line(analyzer._line_to_invoice_dict(l), []) for l in a2.xml_data.righe]

# Caso Electro Rent - scostamento reale
a3 = FatturaPAAnalysis(attachment_id=3)
a3.supplier_name = "Electro Rent"
a3.invoice_total = 5050.26
a3.oda_references_in_xml = ["P03732"]
a3.xml_data = FatturaPAData(
    imponibile_totale=4139.56, imposta_totale=910.70, importo_totale=5050.26,
    righe=[
        FatturaPALine(numero_linea=1, descrizione="Noleggio strumenti A", quantita=1, prezzo_unitario=380.68, prezzo_totale=380.68),
        FatturaPALine(numero_linea=2, descrizione="Noleggio strumenti B", quantita=1, prezzo_unitario=753.91, prezzo_totale=753.91),
        FatturaPALine(numero_linea=3, descrizione="Noleggio strumenti C", quantita=1, prezzo_unitario=3004.97, prezzo_totale=3004.97),
    ]
)
a3.purchase_order = {'id': 3, 'name': 'P03732', 'amount_untaxed': 3515.89,
                     'amount_total': 4289.39, 'state': 'purchase', 'invoice_status': 'to invoice'}
a3.po_lines = []
a3.line_matches = [matcher.match_line(analyzer._line_to_invoice_dict(l), []) for l in a3.xml_data.righe]

# Applico la classificazione
for a in [a1, a2, a3]:
    analyzer._classify(a)
    print(f"\n{a.supplier_name} (OdA {a.oda_references_in_xml[0]}):")
    print(f"  Imponibile: €{a.xml_data.imponibile_totale:.2f} | OdA: €{a.purchase_order['amount_untaxed']:.2f}")
    print(f"  Diff: €{a.total_diff:+.2f} ({a.total_diff_percent:.1f}%)")
    print(f"  => {a.classification}")
    for act in a.actions_suggested:
        print(f"     {act}")
