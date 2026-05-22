"""
One-shot: analisi OdA P04943 + ricerca fattura fornitore RAJAPACK in
e-fatture in ingresso (fatturapa.attachment.in registered=False).
Read-only.
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

print("=" * 80)
print("ODA P04943")
print("=" * 80)
po = client.search_purchase_order_by_name('P04943')
if not po:
    print("NON TROVATO")
else:
    for k in ('id', 'name', 'partner_id', 'date_order', 'state',
              'invoice_status', 'amount_untaxed', 'amount_tax',
              'amount_total', 'company_id', 'currency_id'):
        print(f"  {k}: {po.get(k)}")
    print(f"  n_lines: {len(po.get('order_line') or [])}")

    lines = client.get_purchase_order_lines(po['order_line'])
    print(f"\n  RIGHE OdA ({len(lines)}):")
    for ln in lines:
        prod = ln.get('product_id')
        prod_name = prod[1] if prod else '-'
        print(f"   id={ln['id']:>6} | qty={ln['product_qty']:>6} "
              f"rec={ln['qty_received']:>4} inv={ln['qty_invoiced']:>4} "
              f"| price={ln['price_unit']:>10.2f} | "
              f"subt={ln['price_subtotal']:>10.2f} | "
              f"taxes={ln.get('taxes_id')} | "
              f"{(ln.get('name') or '')[:60]} | prod={prod_name[:30]}")

print("\n" + "=" * 80)
print("FATTURE PASSIVE RAJAPACK in e-fatture in ingresso (registered=False)")
print("=" * 80)

# Trova partner RAJAPACK
partner_ids = client._call('res.partner', 'search_read',
                             [('name', 'ilike', 'rajapack')],
                             fields=['id', 'name', 'vat', 'supplier_rank'],
                             limit=20)
print(f"\nPartner trovati: {len(partner_ids)}")
for p in partner_ids:
    print(f"  id={p['id']} | {p['name']!r} | VAT={p.get('vat')} | "
          f"supplier_rank={p.get('supplier_rank')}")

# Cerca attachment fatturapa per cedente RAJAPACK
# xml_supplier_id punta a res.partner (cedente). Filtro: name ilike
atts = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('registered', '=', False),
     ('is_self_invoice', '=', False),
     ('xml_supplier_id.name', 'ilike', 'rajapack')],
    fields=['id', 'name', 'att_name', 'xml_supplier_id',
            'invoices_total', 'invoices_date', 'invoices_number',
            'company_id', 'create_date', 'inconsistencies',
            'e_invoice_parsing_error', 'e_invoice_validation_error'],
    order='create_date desc',
    limit=20,
)
print(f"\nAttachment RAJAPACK da registrare: {len(atts)}")
for a in atts:
    sup = a.get('xml_supplier_id')
    co = a.get('company_id')
    print(f"  id={a['id']} | {a.get('att_name')!r}")
    print(f"     n.fattura={a.get('invoices_number')} | "
          f"data={a.get('invoices_date')} | tot={a.get('invoices_total')}")
    print(f"     cedente={sup[1] if sup else '-'} | "
          f"company={co[1] if co else '-'} | "
          f"create_date={a.get('create_date')}")
    if a.get('inconsistencies'):
        print(f"     ⚠ inconsistencies: {a['inconsistencies']}")
    if a.get('e_invoice_parsing_error'):
        print(f"     ⚠ parsing_error: {a['e_invoice_parsing_error']}")
    if a.get('e_invoice_validation_error'):
        print(f"     ⚠ validation_error: {a['e_invoice_validation_error']}")
