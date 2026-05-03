import os, sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

# === 1. Struttura OdA P03735 ===
print("="*78)
print("=== STRUTTURA OdA P03735 (EDENRED UTA) ===")
print("="*78)

po = client.search_purchase_order_by_name('P03735')
if not po:
    print("OdA P03735 NON TROVATO")
    sys.exit(1)

print(f"Fornitore: {po['partner_id']}")
print(f"Stato: {po['state']} | invoice_status: {po['invoice_status']}")
print(f"Data OdA: {po.get('date_order', '?')[:10]}")
print(f"Imponibile totale OdA: €{po['amount_untaxed']:,.2f}")
print(f"Totale IVA incl: €{po['amount_total']:,.2f}")

# Righe OdA con dettaglio conto analitico e tasse
line_ids = po.get('order_line', [])
lines = client._call('purchase.order.line', 'read', [line_ids],
    fields=['id', 'name', 'product_id', 'product_qty', 'price_unit',
            'price_subtotal', 'account_analytic_id', 'taxes_id',
            'qty_invoiced', 'qty_received'])

print(f"\nRighe OdA ({len(lines)}):")
print(f"  {'Qty':>5} {'Prezzo':>10} {'Subtotale':>11} {'Fatt':>8} {'Ric':>8} Descrizione")
for l in lines:
    prod = l.get('product_id')
    prod_name = prod[1][:45] if isinstance(prod, list) else '?'
    print(f"  {l.get('product_qty'):>5.1f} €{l.get('price_unit'):>8.2f} "
          f"€{l.get('price_subtotal'):>9.2f} "
          f"{l.get('qty_invoiced', 0):>7.2f} {l.get('qty_received', 0):>7.2f} | {prod_name}")

# === 2. Fatture collegate a P03735 ===
print("\n" + "="*78)
print("=== FATTURE COLLEGATE A P03735 ===")
print("="*78)

moves = client._call('account.move', 'search_read',
    [('invoice_origin', '=', 'P03735'),
     ('move_type', '=', 'in_invoice'),
     ('state', 'in', ['posted', 'draft'])],
    fields=['id', 'name', 'state', 'invoice_date', 'amount_untaxed',
            'amount_total', 'ref', 'invoice_line_ids'],
    order='invoice_date asc', limit=100)

print(f"\nFatture collegate: {len(moves)}")
for m in moves:
    print(f"  [{m['state']}] {m.get('invoice_date')} | {m.get('ref')} | "
          f"€{m.get('amount_untaxed'):.2f} imp / €{m.get('amount_total'):.2f} totale")

# === 3. Dettaglio righe di ciascuna fattura EDENRED ===
print("\n" + "="*78)
print("=== DETTAGLIO RIGHE FATTURE EDENRED (conti contabili usati) ===")
print("="*78)

for m in moves:
    print(f"\n--- Fattura {m.get('ref')} del {m.get('invoice_date')} (€{m.get('amount_untaxed'):.2f}) ---")
    line_ids = m.get('invoice_line_ids', [])
    if not line_ids:
        continue
    aml = client._call('account.move.line', 'read', [line_ids],
        fields=['id', 'name', 'account_id', 'price_subtotal',
                'quantity', 'price_unit', 'display_type'])
    for l in aml:
        if l.get('display_type'):
            continue
        acc = l.get('account_id')
        acc_name = acc[1][:50] if isinstance(acc, list) else '?'
        print(f"    {l.get('quantity'):>6.1f} × €{l.get('price_unit'):>8.4f} "
              f"= €{l.get('price_subtotal'):>9.2f} | {acc_name} | "
              f"{(l.get('name') or '')[:50]}")

# === 4. Quanto è saturo l'OdA ===
total_fatt = sum(m.get('amount_untaxed', 0) or 0 for m in moves
                 if m.get('state') in ('posted', 'draft'))
residuo = po['amount_untaxed'] - total_fatt
consumo_pct = (total_fatt / po['amount_untaxed'] * 100) if po['amount_untaxed'] > 0 else 0

print("\n" + "="*78)
print("=== STATO SATURAZIONE OdA ===")
print("="*78)
print(f"Budget OdA: €{po['amount_untaxed']:,.2f}")
print(f"Fatturato:  €{total_fatt:,.2f} ({consumo_pct:.1f}%)")
print(f"Residuo:    €{residuo:,.2f} ({100-consumo_pct:.1f}% disponibile)")

# === 5. Purchase method (importante per automazione) ===
print("\n" + "="*78)
print("=== CAMPI TECNICI RILEVANTI PER AUTOMAZIONE ===")
print("="*78)
# Stampo campi utili per capire se OdA è automation-ready
po_full = client._call('purchase.order', 'read', [[po['id']]],
    fields=['id', 'name', 'state', 'invoice_status', 'partner_id',
            'amount_untaxed', 'company_id', 'currency_id'])[0]

# Controllo la purchase_method di uno dei prodotti
if lines:
    prod_id = lines[0].get('product_id')
    if isinstance(prod_id, list):
        pid = prod_id[0]
        products = client._call('product.product', 'read', [[pid]],
            fields=['id', 'name', 'purchase_method', 'property_account_expense_id'])
        if products:
            p = products[0]
            print(f"Prodotto riga 1: {p.get('name')}")
            print(f"  purchase_method: {p.get('purchase_method')} "
                  f"(deve essere 'purchase' per fatturazione su quantità ordinate)")
            acc_exp = p.get('property_account_expense_id')
            print(f"  Conto spesa prodotto: {acc_exp}")

print(f"\nCompany: {po_full.get('company_id')}")
print(f"Currency: {po_full.get('currency_id')}")