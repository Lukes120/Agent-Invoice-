import os, sys, base64, sqlite3, logging
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(message)s')

from dotenv import load_dotenv
from core.odoo_rw_client import OdooReadWriteClient
from core.odoo_writer import OdooWriter
from core.fatturapa_analyzer import FatturaPAAnalysis
from core.fatturapa_parser import parse_from_base64
from config.rules import MAPPATURA_FORNITORI_FISSI

# ============================================================
# CONFIGURAZIONE TEST - MODIFICA QUI
# ============================================================
ANALYSIS_ID = 2390
DRY_RUN = False   # False = scrittura reale
# ============================================================

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

# USA IL CLIENT READ-WRITE (non quello read-only!)
client = OdooReadWriteClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

conn = sqlite3.connect('webapp/dashboard.db')
row = conn.execute("SELECT attachment_id, supplier_name, invoice_number "
                   "FROM analyses WHERE id=?", (ANALYSIS_ID,)).fetchone()
conn.close()
if not row:
    print(f"ERRORE: Analisi {ANALYSIS_ID} non trovata nel DB")
    sys.exit(1)

att_id = row[0]
print(f"Analisi: {ANALYSIS_ID}")
print(f"Fornitore: {row[1]}")
print(f"Fattura: {row[2]}")
print(f"Attachment id: {att_id}")
print(f"DRY_RUN: {DRY_RUN}")
print()

if not DRY_RUN:
    print("=" * 60)
    print("ATTENZIONE: Questo test creerà una BOZZA REALE in Odoo.")
    print("Premere INVIO per procedere, CTRL+C per annullare.")
    print("=" * 60)
    input()

atts = client._call('fatturapa.attachment.in', 'search_read',
    [('id', '=', att_id)],
    fields=['id', 'name', 'datas'])
att = atts[0]

a = FatturaPAAnalysis(attachment_id=att_id)
a.xml_data = parse_from_base64(att['datas'])
a.raw_xml = base64.b64decode(att['datas']).decode('utf-8', errors='replace')
a.classification = 'MAPPATURA_FORNITORE_FISSO'

mapping = MAPPATURA_FORNITORI_FISSI['IT05403151003']

writer = OdooWriter(client, dry_run=DRY_RUN)
result = writer.create_bozza_fornitore_fisso(a, mapping)

print("\n" + "=" * 60)
print("RISULTATO:")
print("=" * 60)
print(f"Success: {result.success}")
print(f"Action: {result.action}")
print(f"Dry run: {result.dry_run}")
print(f"PO line aggiornata: id={result.po_line_id}")
print(f"  old_price_unit: {result.old_price_unit}")
print(f"  old_name: {result.old_name}")
print(f"Move creato: id={result.move_id}")
print(f"Error: {result.error_message}")

if result.success and not result.dry_run:
    print(f"\n=== BOZZA CREATA CON SUCCESSO ===")
    print(f"Verifica in Odoo: account.move id {result.move_id}")
    print(f"URL: https://odoo.ecotelitalia.it/web#id={result.move_id}&model=account.move")