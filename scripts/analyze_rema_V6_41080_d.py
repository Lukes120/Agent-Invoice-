"""
Analisi OdA P04679 + classificazione corrente fattura V6/2026/000041080.
"""
import sys
import os
import sqlite3
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'],
    db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'],
    password=os.environ['ODOO_PASSWORD'],
)
client.connect()

ODA = 'P04679'
ATT_ID = 5351680
print('=' * 90)
print(f'OdA {ODA}')
print('=' * 90)
po = client.search_purchase_order_by_name(ODA)
if not po:
    print('NON TROVATO')
    sys.exit(1)
for k in ('id', 'name', 'partner_id', 'date_order', 'state',
          'invoice_status', 'amount_untaxed', 'amount_tax',
          'amount_total', 'company_id', 'currency_id'):
    print(f'  {k}: {po.get(k)}')

extra = client._call('purchase.order', 'read', [po['id']],
                    fields=['origin', 'notes', 'user_id', 'date_planned'])
if extra:
    for k, v in extra[0].items():
        if k == 'id':
            continue
        print(f'  {k}: {v}')

print(f"\n  n_lines: {len(po.get('order_line') or [])}")
lines = client.get_purchase_order_lines(po['order_line'])
free = []
used = []
for ln in lines:
    qty = ln.get('product_qty') or 0
    rec = ln.get('qty_received') or 0
    inv = ln.get('qty_invoiced') or 0
    pu = ln.get('price_unit') or 0
    sub = ln.get('price_subtotal') or 0
    flag = 'LIBERA ' if (inv == 0 and rec == 0) else 'USATA '
    if inv == 0 and rec == 0:
        free.append(ln)
    else:
        used.append(ln)
    prod = ln.get('product_id')
    prod_name = prod[1] if prod else '-'
    print(f"   {flag} id={ln['id']:>6} | qty={qty:>5} rec={rec:>4} inv={inv:>4} "
          f"| pu={pu:>10.2f} | subt={sub:>10.2f} | tax={ln.get('taxes_id')} | "
          f"{(ln.get('name') or '')[:55]}")
print(f"\n  libere: {len(free)} | usate: {len(used)}")

# fatture posted gia' collegate
cum = client.get_invoiced_amount_for_po(po['id'], po_name=ODA)
print(f"\n  Fatture gia' collegate a {ODA}:")
print(f"     posted (imponibile): {cum['already_invoiced_posted']:.2f}")
print(f"     draft  (imponibile): {cum['already_invoiced_draft']:.2f}")
print(f"     n fatture: {cum['count_invoices']}")
for inv in cum.get('invoices_info', [])[:15]:
    print(f"     - {inv['state']:>6} | {inv.get('date')} | "
          f"{inv['name']!r} | imp={inv['amount']:.2f}")

# Classificazione corrente del fattura sul dashboard.db
# (DB locale o sul server? Cerco entrambi)
DB_PATHS = [
    ROOT / 'webapp' / 'dashboard.db',
    Path(r'C:\Users\lranalletta\Documents\AGENT FATTURAZIONE PASSIVA\odoo_invoice_agent\webapp\dashboard.db'),
    Path(r'C:\Users\lranalletta\Documents\AGENT FATTURAZIONE PASSIVA\odoo_invoice_agent\Agent Invoices\dashboard.db'),
]
print()
print('=' * 90)
print('Classificazione corrente del fattura su dashboard.db locale')
print('=' * 90)
for dbp in DB_PATHS:
    if not dbp.exists():
        continue
    print(f"DB: {dbp}")
    conn = sqlite3.connect(str(dbp))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """SELECT id, run_id, classification, fattura_numero, fattura_data,
               importo_totale, oda_match, json_extract(json_data, '$.actions_suggested') as actions,
               json_extract(json_data, '$.warnings') as warnings
           FROM analyses
           WHERE attachment_id = ?
           ORDER BY id DESC LIMIT 5""", (ATT_ID,))
    found = 0
    for r in rows:
        found += 1
        print(f"\n  analysis_id={r['id']} run={r['run_id']}")
        print(f"    classification: {r['classification']}")
        print(f"    numero/data    : {r['fattura_numero']} / {r['fattura_data']}")
        print(f"    importo        : {r['importo_totale']}")
        print(f"    oda match      : {r['oda_match']}")
        print(f"    actions        : {r['actions']}")
        print(f"    warnings       : {r['warnings']}")
    if not found:
        print(f"  Nessuna analisi trovata per attachment {ATT_ID} in questo DB")
    conn.close()
    break
else:
    print("Nessun dashboard.db locale trovato; classificazione su server.")

print('\nDONE.')
