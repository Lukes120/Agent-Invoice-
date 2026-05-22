"""Smoke test Fix A: classifica la fattura RAJAPACK 5351870 e stampa il path."""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_analyzer import FatturaPAAnalyzer
from core.matcher import InvoiceMatcher
from core.keyword_rules import classify_line_by_keyword
from config.rules import (
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA, TOLLERANZA_TOTALE_FATTURA,
    MATCH_IMPLICITO_ATTIVO, TOLLERANZA_MATCH_IMPLICITO_PERCENT,
    TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA, TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA,
    TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT, MATCH_PARZIALE_ATTIVO,
    SUGGERIMENTI_ATTIVI, MAPPATURA_FORNITORI_FISSI_ATTIVA, MAPPATURA_FORNITORI_FISSI,
)

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'], db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'], password=os.environ['ODOO_PASSWORD'])
client.connect()

matcher = InvoiceMatcher(
    tol_percent=TOLLERANZA_PERCENTUALE, tol_absolute=TOLLERANZA_ASSOLUTA,
    tol_total=TOLLERANZA_TOTALE_FATTURA, keyword_classifier=classify_line_by_keyword)

analyzer = FatturaPAAnalyzer(
    client, matcher, TOLLERANZA_TOTALE_FATTURA,
    implicit_match_enabled=MATCH_IMPLICITO_ATTIVO,
    implicit_match_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_PERCENT,
    implicit_match_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
    implicit_match_loose_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA,
    implicit_match_loose_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
    partial_match_enabled=MATCH_PARZIALE_ATTIVO,
    suggestions_enabled=SUGGERIMENTI_ATTIVI,
    supplier_mapping_enabled=MAPPATURA_FORNITORI_FISSI_ATTIVA,
    supplier_mapping=MAPPATURA_FORNITORI_FISSI)

ATT_FIELDS = ['id', 'name', 'datas', 'invoices_total', 'invoices_date',
              'invoices_number', 'xml_supplier_id', 'inconsistencies',
              'e_invoice_parsing_error', 'create_date']

att = client._call('fatturapa.attachment.in', 'read', [5351870],
                   fields=ATT_FIELDS)[0]

analysis = analyzer.analyze(att)

print("=" * 80)
print(f"RAJAPACK 5351870 (FV-26035803) — Fix A smoke test")
print("=" * 80)
print(f"  classification:      {analysis.classification}")
print(f"  oda_references_xml:  {analysis.oda_references_in_xml}")
po = analysis.purchase_order
print(f"  purchase_order:      {po.get('name') if po else None} "
      f"(amount_untaxed={po.get('amount_untaxed') if po else None})")
print(f"  total_diff:          {analysis.total_diff}")
print(f"  total_diff_percent:  {analysis.total_diff_percent:.2f}%")
print(f"  partial_match_applied: {analysis.partial_match_applied}")
print(f"  partial_match_subset_lines: {analysis.partial_match_subset_lines}")
print(f"  partial_extra_lines: {len(analysis.partial_extra_lines)}")
print()
print("  line_matches:")
for i, lm in enumerate(analysis.line_matches, 1):
    pol = lm.po_line
    pol_lbl = f"POL {pol.get('id')} '{(pol.get('name') or '')[:30]}'" if pol else "-"
    print(f"   {i}. type={lm.match_type:<10} "
          f"desc={(lm.invoice_line.get('name') or '')[:45]!r} "
          f"imp={lm.invoice_line.get('price_subtotal')} "
          f"po_line={pol_lbl}")
    for note in lm.notes:
        print(f"      note: {note}")
print()
print("  actions_suggested:")
for a in analysis.actions_suggested:
    print(f"   - {a}")
print()
print("  warnings:")
for w in analysis.warnings:
    print(f"   - {w}")

print()
print("=" * 80)
expected = "AUTO_VALIDABILE"
status = "OK" if analysis.classification == expected else "FAIL"
print(f"  EXPECTED {expected} -> GOT {analysis.classification} : {status}")
print("=" * 80)
