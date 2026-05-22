"""
Parte D: ricontrollo righe LIBERE P03696 dopo correzione tax acquisti.
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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

po = client.search_purchase_order_by_name('P03696')
lines = client.get_purchase_order_lines(po['order_line'])

print("=" * 100)
print("RIGHE LIBERE P03696 - VERIFICA TAX (post correzione)")
print("=" * 100)
print(f"{'id':>7} | {'tipo':<22} | {'mese':<11} | {'price':>9} | {'tax':<8} | nome")
print("-" * 100)

# mesi label
import re
MONTHS = ['Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
         'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre']

free_oneri = []
free_fee = []

for ln in lines:
    if ln.get('qty_invoiced') or ln.get('qty_received'):
        continue  # skip chiuse/parziali
    name = ln.get('name') or ''
    pu = ln.get('price_unit') or 0
    tax = ln.get('taxes_id') or []
    if 'Oneri' in name or 'oneri' in name.lower():
        tipo = 'ONERI FACTORING'
    elif 'FEE' in name:
        tipo = 'FEE'
    else:
        tipo = '?'
    # mese
    mese = '-'
    for m in MONTHS:
        if m in name:
            mese = m
            break
    print(f"{ln['id']:>7} | {tipo:<22} | {mese:<11} | {pu:>9.2f} | {str(tax):<8} | {name[:55]}")
    if tipo == 'ONERI FACTORING':
        free_oneri.append((ln['id'], mese, pu, tax))
    elif tipo == 'FEE':
        free_fee.append((ln['id'], mese, pu, tax))

print("\nRIGHE LIBERE 'Oneri Factoring' per mese di MAGGIO:")
for tup in free_oneri:
    if tup[1] == 'Maggio':
        print(f"  POL {tup[0]} | tax={tup[3]} | price={tup[2]}")

print("\nRIGHE LIBERE 'FEE' per mese di MAGGIO:")
for tup in free_fee:
    if tup[1] == 'Maggio':
        print(f"  POL {tup[0]} | tax={tup[3]} | price={tup[2]}")

# Recupero descrizione tax 54 e 11 per chiarezza
print("\n--- mappa tax_id -> nome ---")
tax_ids = sorted({t for tup in (free_oneri + free_fee) for t in tup[3]})
if tax_ids:
    taxes = client._call('account.tax', 'read', tax_ids,
                        fields=['id', 'name', 'amount', 'description', 'type_tax_use'])
    for t in taxes:
        print(f"  tax_id={t['id']} | name={t['name']!r} | amount={t['amount']} | desc={t.get('description')!r}")

print("\nDONE.")
