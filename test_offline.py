"""
Test offline con fatture simulate.
Verifica che la pipeline funzioni end-to-end senza bisogno di Odoo reale.
"""
import sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.matcher import InvoiceMatcher, InvoiceAnalysis
from core.classifier import InvoiceClassifier
from core.keyword_rules import classify_line_by_keyword
from reports.dashboard import generate_dashboard
from reports.excel_report import generate_excel

# ---- FATTURE SIMULATE ----

# Caso 1: perfetto match
inv1 = {
    'id': 1, 'name': 'BILL/2026/0001', 'ref': 'FT-1234',
    'partner_id': [10, 'Rossi SpA'], 'invoice_date': '2026-04-10',
    'amount_untaxed': 1000.00, 'amount_tax': 220.00, 'amount_total': 1220.00,
    'invoice_origin': 'P01234', 'state': 'draft',
}
inv1_lines = [
    {'id': 101, 'name': 'Bulloni M6', 'product_id': [500, 'Bulloni M6'],
     'quantity': 100, 'price_unit': 10.0, 'price_subtotal': 1000.0,
     'purchase_line_id': [901]},
]
po1 = {'id': 901, 'name': 'P01234', 'amount_untaxed': 1000.00,
       'amount_total': 1220.00, 'order_line': [901]}
po1_lines = [
    {'id': 901, 'product_id': [500, 'Bulloni M6'], 'product_qty': 100,
     'price_unit': 10.0, 'price_subtotal': 1000.0},
]

# Caso 2: differenza entro tolleranza
inv2 = {
    'id': 2, 'name': 'BILL/2026/0002', 'ref': 'FT-1235',
    'partner_id': [11, 'Bianchi Srl'], 'invoice_date': '2026-04-12',
    'amount_untaxed': 1002.50, 'amount_tax': 220.55, 'amount_total': 1223.05,
    'invoice_origin': 'P01235', 'state': 'draft',
}
inv2_lines = [
    {'id': 201, 'name': 'Dadi M6', 'product_id': [501, 'Dadi M6'],
     'quantity': 100, 'price_unit': 10.025, 'price_subtotal': 1002.50,
     'purchase_line_id': [902]},
]
po2 = {'id': 902, 'name': 'P01235', 'amount_untaxed': 1000.00,
       'amount_total': 1220.00, 'order_line': [902]}
po2_lines = [
    {'id': 902, 'product_id': [501, 'Dadi M6'], 'product_qty': 100,
     'price_unit': 10.0, 'price_subtotal': 1000.0},
]

# Caso 3: merce + trasporto
inv3 = {
    'id': 3, 'name': 'BILL/2026/0003', 'ref': 'FT-9999',
    'partner_id': [12, 'Verdi Industrie'], 'invoice_date': '2026-04-15',
    'amount_untaxed': 1050.00, 'amount_tax': 231.00, 'amount_total': 1281.00,
    'invoice_origin': 'P01236', 'state': 'draft',
}
inv3_lines = [
    {'id': 301, 'name': 'Viti autofilettanti', 'product_id': [502, 'Viti'],
     'quantity': 200, 'price_unit': 5.0, 'price_subtotal': 1000.0,
     'purchase_line_id': [903]},
    {'id': 302, 'name': 'SPESE DI TRASPORTO E SPEDIZIONE',
     'product_id': False, 'quantity': 1, 'price_unit': 50.0,
     'price_subtotal': 50.0, 'purchase_line_id': False},
]
po3 = {'id': 903, 'name': 'P01236', 'amount_untaxed': 1000.00,
       'amount_total': 1220.00, 'order_line': [903]}
po3_lines = [
    {'id': 903, 'product_id': [502, 'Viti'], 'product_qty': 200,
     'price_unit': 5.0, 'price_subtotal': 1000.0},
]

# Caso 4: scostamento fuori tolleranza
inv4 = {
    'id': 4, 'name': 'BILL/2026/0004', 'ref': 'FT-5555',
    'partner_id': [13, 'Neri Componenti'], 'invoice_date': '2026-03-20',
    'amount_untaxed': 1250.00, 'amount_tax': 275.00, 'amount_total': 1525.00,
    'invoice_origin': 'P01237', 'state': 'draft',
}
inv4_lines = [
    {'id': 401, 'name': 'Cuscinetti 6205', 'product_id': [503, 'Cuscinetto'],
     'quantity': 50, 'price_unit': 25.0, 'price_subtotal': 1250.0,
     'purchase_line_id': [904]},
]
po4 = {'id': 904, 'name': 'P01237', 'amount_untaxed': 1000.00,
       'amount_total': 1220.00, 'order_line': [904]}
po4_lines = [
    {'id': 904, 'product_id': [503, 'Cuscinetto'], 'product_qty': 50,
     'price_unit': 20.0, 'price_subtotal': 1000.0},
]

# Caso 5: OdA mancante
inv5 = {
    'id': 5, 'name': 'BILL/2026/0005', 'ref': 'FT-7777',
    'partner_id': [14, 'Gialli Srl'], 'invoice_date': '2026-04-17',
    'amount_untaxed': 500.00, 'amount_tax': 110.00, 'amount_total': 610.00,
    'invoice_origin': 'P99999', 'state': 'draft',
}
inv5_lines = [
    {'id': 501, 'name': 'Materiali vari', 'product_id': False,
     'quantity': 1, 'price_unit': 500.0, 'price_subtotal': 500.0,
     'purchase_line_id': False},
]

# Caso 6: solo trasporto, senza OdA (corriere)
inv6 = {
    'id': 6, 'name': 'BILL/2026/0006', 'ref': 'FT-8888',
    'partner_id': [15, 'BRT Corriere'], 'invoice_date': '2026-04-16',
    'amount_untaxed': 80.00, 'amount_tax': 17.60, 'amount_total': 97.60,
    'invoice_origin': False, 'state': 'draft',
}
inv6_lines = [
    {'id': 601, 'name': 'Servizio di consegna espresso',
     'product_id': False, 'quantity': 1, 'price_unit': 80.0,
     'price_subtotal': 80.0, 'purchase_line_id': False},
]

# ---- ESECUZIONE PIPELINE ----

matcher = InvoiceMatcher(
    tol_percent=2.0, tol_absolute=5.0, tol_total=10.0,
    keyword_classifier=classify_line_by_keyword,
)
classifier = InvoiceClassifier()

cases = [
    (inv1, inv1_lines, po1, po1_lines),
    (inv2, inv2_lines, po2, po2_lines),
    (inv3, inv3_lines, po3, po3_lines),
    (inv4, inv4_lines, po4, po4_lines),
    (inv5, inv5_lines, None, []),
    (inv6, inv6_lines, None, []),
]

analyses = []
for inv, il, po, pol in cases:
    a = InvoiceAnalysis(invoice=inv, purchase_order=po)
    for line in il:
        a.line_matches.append(matcher.match_line(line, pol))
    classifier.classify(a)
    analyses.append(a)
    print(f"\n{inv['name']} ({inv['partner_id'][1]})")
    print(f"  -> {a.classification}  (priority {a.priority_score})")
    for act in a.actions_suggested:
        print(f"     {act}")

# ---- OUTPUT ----
from datetime import datetime
out_dir = Path(__file__).parent / 'output' / 'test_simulato'
out_dir.mkdir(parents=True, exist_ok=True)

generate_dashboard(analyses, str(out_dir / 'dashboard.html'), "TEST SIMULATO")
generate_excel(analyses, str(out_dir / 'report_dettagliato.xlsx'))

print(f"\nOutput generati in: {out_dir}")
