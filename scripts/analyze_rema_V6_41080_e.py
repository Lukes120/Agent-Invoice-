"""
Verifica picking/stock.move collegati all'OdA P04679.
"""
import sys
import os
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

PO_ID = 19302  # P04679
PO_NAME = 'P04679'

# 1) PO con picking_ids
po = client._call('purchase.order', 'read', [PO_ID],
                  fields=['id', 'name', 'state', 'picking_ids',
                          'invoice_status', 'picking_count',
                          'order_line', 'origin'])
po = po[0]
print(f"OdA {po['name']} | state={po['state']} | inv_status={po['invoice_status']} "
      f"| picking_count={po.get('picking_count')} | picking_ids={po.get('picking_ids')}")

picking_ids = po.get('picking_ids') or []
if not picking_ids:
    print("\nNESSUN picking collegato all'OdA.")
else:
    pickings = client._call(
        'stock.picking', 'read', picking_ids,
        fields=['id', 'name', 'state', 'scheduled_date',
                'date_done', 'origin', 'partner_id', 'picking_type_id',
                'location_id', 'location_dest_id', 'move_lines',
                'note'])
    print(f"\nPicking trovati: {len(pickings)}")
    for p in pickings:
        print(f"\n  picking id={p['id']} | {p['name']!r}")
        print(f"     state         : {p.get('state')}")
        print(f"     scheduled     : {p.get('scheduled_date')}")
        print(f"     date_done     : {p.get('date_done')}")
        print(f"     origin        : {p.get('origin')}")
        print(f"     partner       : {p.get('partner_id')}")
        print(f"     picking_type  : {p.get('picking_type_id')}")
        print(f"     from_loc      : {p.get('location_id')}")
        print(f"     to_loc        : {p.get('location_dest_id')}")
        print(f"     n.move_lines  : {len(p.get('move_lines') or [])}")
        if p.get('note'):
            print(f"     note          : {p.get('note')}")
        if p.get('move_lines'):
            moves = client._call(
                'stock.move', 'read', p['move_lines'],
                fields=['id', 'name', 'state', 'product_id',
                        'product_uom_qty', 'quantity_done', 'reserved_availability',
                        'purchase_line_id', 'date'])
            print(f"     STOCK.MOVE:")
            for m in moves:
                pol = m.get('purchase_line_id')
                pol_id = pol[0] if pol else None
                print(f"       sm id={m['id']} | state={m.get('state'):<10} "
                      f"| qty_ord={m.get('product_uom_qty')} "
                      f"qty_done={m.get('quantity_done')} "
                      f"qty_avail={m.get('reserved_availability')} "
                      f"| POL={pol_id} | {(m.get('name') or '')[:50]}")
                print(f"             date={m.get('date')}")

# 2) Verifica qty_received sulle POL una volta in piu' con purchase_method
print()
print('=' * 90)
print('POL details + product.purchase_method')
print('=' * 90)
po_lines_full = client._call('purchase.order.line', 'read', po.get('order_line'),
                            fields=['id', 'name', 'product_id', 'product_qty',
                                    'qty_received', 'qty_received_method',
                                    'qty_invoiced', 'price_unit'])
for ln in po_lines_full:
    prod = ln.get('product_id')
    print(f"  POL id={ln['id']} | qty={ln.get('product_qty')} "
          f"rec={ln.get('qty_received')} inv={ln.get('qty_invoiced')} "
          f"| method={ln.get('qty_received_method')} "
          f"| product={prod[1] if prod else '-'}")
    if prod:
        p = client._call('product.product', 'read', [prod[0]],
                        fields=['id', 'name', 'purchase_method', 'type',
                                'invoice_policy', 'categ_id'])
        if p:
            print(f"     product.purchase_method = {p[0].get('purchase_method')} "
                  f"| type={p[0].get('type')} "
                  f"| categ={p[0].get('categ_id')}")

print('\nDONE.')
