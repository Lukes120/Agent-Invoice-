"""Diagnostico Lyreco: per ogni fattura pending mostra classificazione,
OdA matchato, OdA esplicito citato in XML, suggerimenti, warning e righe.
Riusa la pipeline completa dell'analyzer (read-only).
"""
import io
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.matcher import InvoiceMatcher
from core.fatturapa_analyzer import FatturaPAAnalyzer
from core.keyword_rules import classify_line_by_keyword
from config.rules import (
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA, TOLLERANZA_TOTALE_FATTURA,
    TOLLERANZA_MATCH_IMPLICITO_PERCENT, TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
    TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA, TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
    MATCH_IMPLICITO_GUARDIA_DUPLICATI, MATCH_IMPLICITO_ATTIVO,
    MATCH_PARZIALE_ATTIVO, MATCH_PARZIALE_MAX_RIGHE,
    MATCH_PARZIALE_MAX_EXTRA_PERCENT, MATCH_PARZIALE_TOLLERANZA_ASSOLUTA,
    SUGGERIMENTI_ATTIVI, SUGGERIMENTI_MAX_RIGHE,
    SUGGERIMENTI_TOLLERANZA_ASSOLUTA, SUGGERIMENTI_MAX_AGE_MONTHS,
    MAPPATURA_FORNITORI_FISSI, MAPPATURA_FORNITORI_FISSI_ATTIVA,
)

FILTRO = 'LYRECO'

client = OdooReadOnlyClient(
    os.environ['ODOO_URL'], os.environ['ODOO_DB'],
    os.environ['ODOO_USERNAME'], os.environ['ODOO_PASSWORD'],
)
client.connect()

attachments = client.get_fatturapa_attachments(
    only_unregistered=True, exclude_self_invoice=True, company_id=1,
)

matcher = InvoiceMatcher(
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA,
    TOLLERANZA_TOTALE_FATTURA, classify_line_by_keyword,
)
analyzer = FatturaPAAnalyzer(
    client, matcher, TOLLERANZA_TOTALE_FATTURA,
    implicit_match_enabled=MATCH_IMPLICITO_ATTIVO,
    implicit_match_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_PERCENT,
    implicit_match_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
    implicit_match_loose_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA,
    implicit_match_loose_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
    implicit_match_duplicate_guard=MATCH_IMPLICITO_GUARDIA_DUPLICATI,
    partial_match_enabled=MATCH_PARZIALE_ATTIVO,
    partial_match_max_rows=MATCH_PARZIALE_MAX_RIGHE,
    partial_match_max_extra_percent=MATCH_PARZIALE_MAX_EXTRA_PERCENT,
    partial_match_tolerance_absolute=MATCH_PARZIALE_TOLLERANZA_ASSOLUTA,
    suggestions_enabled=SUGGERIMENTI_ATTIVI,
    suggestions_max_lines=SUGGERIMENTI_MAX_RIGHE,
    suggestions_tolerance_absolute=SUGGERIMENTI_TOLLERANZA_ASSOLUTA,
    suggestions_max_age_months=SUGGERIMENTI_MAX_AGE_MONTHS,
    supplier_mapping_enabled=MAPPATURA_FORNITORI_FISSI_ATTIVA,
    supplier_mapping=MAPPATURA_FORNITORI_FISSI,
)

analyses = []
for att in attachments:
    try:
        a = analyzer.analyze(att)
    except Exception as e:
        continue
    analyses.append(a)

analyzer.apply_duplicate_guard(analyses)
analyzer.apply_strict_wins_over_loose(analyses)
analyzer.apply_run_cumulative_check(analyses)

lyreco = [a for a in analyses if FILTRO in (a.supplier_name or '').upper()]
print(f"=== Pending {FILTRO}: {len(lyreco)} ===\n")

for a in sorted(lyreco, key=lambda x: x.invoice_total):
    po = getattr(a, 'purchase_order', None)
    oda_match = po.get('name') if (po and isinstance(po, dict)) else '-'
    oda_xml = getattr(a, 'oda_references_in_xml', []) or getattr(a, 'oda_values_raw', [])
    sugg = getattr(a, 'suggested_pos', []) or []
    warns = getattr(a, 'warnings', []) or []
    print(f"--- Att {a.attachment_id} | Ft {a.invoice_number} | {a.invoice_date[:10]} "
          f"| Totale {a.invoice_total:,.2f} | {a.classification} ---")
    print(f"  OdA matchato:        {oda_match}")
    print(f"  OdA citato in XML:   {oda_xml if oda_xml else '(nessuno)'}")
    if getattr(a, 'partial_extra_lines', None):
        tot = getattr(a, 'partial_extra_total', 0) or 0
        print(f"  Righe accessorie extra: {len(a.partial_extra_lines)} (tot {tot:,.2f})")
    if sugg:
        print(f"  Suggerimenti ({len(sugg)}):")
        for s in sugg[:5]:
            print(f"     - {s.get('po_name')} residuo {s.get('po_residual', s.get('residual','?'))}")
    if warns:
        for w in warns:
            print(f"  ! {w}")
    # righe XML
    xd = getattr(a, 'xml_data', None)
    if xd and getattr(xd, 'righe', None):
        print(f"  Righe XML ({len(xd.righe)}):")
        for ln in xd.righe[:8]:
            print(f"     [{ln.numero_linea}] {(ln.descrizione or '')[:55]:55s} {ln.prezzo_totale:>9,.2f}")
        if len(xd.righe) > 8:
            print(f"     ... (+{len(xd.righe)-8} righe)")
    print()
