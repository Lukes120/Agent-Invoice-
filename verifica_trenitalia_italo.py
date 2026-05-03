import os, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

oda_da_verificare = [
    ('P03524', 'Trenitalia'),
    ('P04279', 'Italo NTV'),
]

for oda_name, label in oda_da_verificare:
    print(f"\n{'='*78}")
    print(f"=== OdA {oda_name} ({label}) ===")
    print('='*78)

    po = client.search_purchase_order_by_name(oda_name)
    if not po:
        print(f"OdA {oda_name} NON TROVATO")
        continue

    print(f"ID OdA: {po['id']}")
    print(f"Fornitore: {po['partner_id']}")
    print(f"Stato: {po['state']} | invoice_status: {po['invoice_status']}")
    print(f"Data OdA: {po.get('date_order', '?')[:10]}")
    print(f"Imponibile totale: €{po['amount_untaxed']:,.2f}")
    print(f"Totale IVA incl: €{po['amount_total']:,.2f}")

    # Leggo le righe - USO search_read invece di read
    line_ids = po.get('order_line', [])
    if not line_ids:
        print("Nessuna riga OdA")
        continue

    lines = client._call('purchase.order.line', 'search_read',
        [('id', 'in', line_ids)],
        fields=['id', 'name', 'product_id', 'product_qty', 'price_unit',
                'price_subtotal', 'account_analytic_id', 'taxes_id',
                'qty_invoiced', 'qty_received'])

    print(f"\nRighe OdA ({len(lines)}):")
    for l in lines:
        prod = l.get('product_id')
        prod_name = prod[1][:45] if isinstance(prod, list) else '?'
        desc = (l.get('name') or '')[:50]
        print(f"  qty={l.get('product_qty'):<6.1f} €{l.get('price_unit'):<10.4f} "
              f"= €{l.get('price_subtotal'):<10.2f} | {prod_name}")
        print(f"       desc: {desc}")
        print(f"       fatturato: {l.get('qty_invoiced', 0)} | ricevuto: {l.get('qty_received', 0)}")
        # Info prodotto (purchase_method, conto spesa)
        if isinstance(prod, list):
            prod_info = client._call('product.product', 'search_read',
                [('id', '=', prod[0])],
                fields=['purchase_method', 'property_account_expense_id'])
            if prod_info:
                pi = prod_info[0]
                print(f"       purchase_method: {pi.get('purchase_method')}")
                print(f"       conto spesa prodotto: {pi.get('property_account_expense_id')}")

    # Fatture collegate
    moves = client._call('account.move', 'search_read',
        [('invoice_origin', '=', oda_name),
         ('move_type', '=', 'in_invoice'),
         ('state', 'in', ['posted', 'draft'])],
        fields=['id', 'name', 'state', 'invoice_date', 'amount_untaxed', 'ref'],
        order='invoice_date asc', limit=200)

    total_fatt = sum(m.get('amount_untaxed', 0) or 0 for m in moves)
    residuo = po['amount_untaxed'] - total_fatt

    print(f"\nFatture collegate: {len(moves)}")
    print(f"Fatturato totale: €{total_fatt:,.2f}")
    print(f"Residuo OdA: €{residuo:,.2f} "
          f"({residuo/po['amount_untaxed']*100:.1f}% disponibile)")
    
    if moves:
        print(f"Prima fattura: {moves[0].get('invoice_date')}")
        print(f"Ultima fattura: {moves[-1].get('invoice_date')}")

    # Campi fattura-ready dell'OdA (ci servono per automazione)
    po_details = client._call('purchase.order', 'search_read',
        [('id', '=', po['id'])],
        fields=['company_id', 'currency_id', 'fiscal_position_id',
                'payment_term_id', 'partner_ref'])[0]
    print(f"\nCampi per automazione:")
    print(f"  company_id: {po_details.get('company_id')}")
    print(f"  currency_id: {po_details.get('currency_id')}")
    print(f"  fiscal_position_id: {po_details.get('fiscal_position_id')}")
    print(f"  payment_term_id: {po_details.get('payment_term_id')}")
    
    # Trovo il journal "Fatture fornitori" che useremmo per le bozze
    journals = client._call('account.journal', 'search_read',
        [('type', '=', 'purchase'),
         ('company_id', '=', po_details.get('company_id', [1, ''])[0])],
        fields=['id', 'name', 'code', 'default_debit_account_id', 'default_credit_account_id'])
    print(f"\nJournal acquisto disponibili:")
    for j in journals:
        print(f"  id={j['id']} | {j['name']} ({j['code']})")