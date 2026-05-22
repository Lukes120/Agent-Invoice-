"""
Allargo la ricerca: tutti gli attachment Rema Tarlazzi (registered or not).
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

# Cerca partner Rema Tarlazzi
partners = client._call('res.partner', 'search_read',
                       [('|'), ('name', 'ilike', 'rema'), ('name', 'ilike', 'tarlazzi')],
                       fields=['id', 'name', 'vat'], limit=10)
print(f"Partner Rema/Tarlazzi: {len(partners)}")
for p in partners:
    print(f"  id={p['id']} | {p['name']!r} | VAT={p.get('vat')}")

# Cerca attachment per cedente
partner_ids = [p['id'] for p in partners]
if partner_ids:
    atts = client._call(
        'fatturapa.attachment.in', 'search_read',
        [('xml_supplier_id', 'in', partner_ids)],
        fields=['id', 'att_name', 'xml_supplier_id',
                'invoices_total', 'invoices_date', 'invoices_number',
                'registered', 'is_self_invoice', 'in_invoice_ids',
                'create_date', 'company_id'],
        order='create_date desc',
        limit=40,
    )
    print(f"\nAttachment Rema (ultimi 40 per data ricezione): {len(atts)}")
    print(f"{'id':>8} | {'reg':<4} | n.fattura            | data       | tot       | company")
    print('-' * 100)
    for a in atts:
        reg = 'SI' if a.get('registered') else 'NO'
        co = a.get('company_id')
        n = str(a.get('invoices_number') or '')[:20]
        tot = a.get('invoices_total') or 0
        print(f"  {a['id']:>6} | {reg:<4} | {n:<20} | {a.get('invoices_date')} | "
              f"{tot:>8} | {co[1] if co else '-'}")

# Cerca attachment con "41080" in qualsiasi campo
print("\nRicerca '41080' tra att_name e invoices_number...")
atts2 = client._call(
    'fatturapa.attachment.in', 'search_read',
    ['|', ('att_name', 'ilike', '41080'), ('invoices_number', 'ilike', '41080')],
    fields=['id', 'att_name', 'invoices_number', 'xml_supplier_id',
            'invoices_date', 'registered', 'company_id'],
    order='create_date desc',
    limit=20,
)
print(f"Trovati: {len(atts2)}")
for a in atts2:
    sup = a.get('xml_supplier_id')
    co = a.get('company_id')
    print(f"  id={a['id']} | reg={a.get('registered')} | n={a.get('invoices_number')!r} | "
          f"att={a.get('att_name')!r}")
    print(f"     cedente: {sup[1] if sup else '-'} | company: {co[1] if co else '-'}")

# Cerca anche move account per il numero V6
print("\nRicerca su account.move con ref ilike 'V6/2026/000041080'...")
moves = client._call(
    'account.move', 'search_read',
    [('ref', 'ilike', 'V6/2026/000041080')],
    fields=['id', 'name', 'ref', 'partner_id', 'invoice_date', 'state',
            'amount_total', 'amount_untaxed', 'company_id', 'invoice_origin'],
    order='invoice_date desc',
    limit=10,
)
print(f"account.move trovate: {len(moves)}")
for m in moves:
    p = m.get('partner_id')
    co = m.get('company_id')
    print(f"  {m['name']} | ref={m.get('ref')} | state={m.get('state')} | "
          f"partner={p[1] if p else '-'} | tot={m.get('amount_total')} | "
          f"company={co[1] if co else '-'} | origin={m.get('invoice_origin')}")

# Anche solo "41080" in ref
print("\nRicerca su account.move con ref ilike '41080'...")
moves2 = client._call(
    'account.move', 'search_read',
    [('ref', 'ilike', '41080'), ('move_type', '=', 'in_invoice')],
    fields=['id', 'name', 'ref', 'partner_id', 'invoice_date', 'state',
            'amount_total', 'company_id', 'invoice_origin'],
    order='invoice_date desc',
    limit=10,
)
print(f"account.move (41080): {len(moves2)}")
for m in moves2:
    p = m.get('partner_id')
    co = m.get('company_id')
    print(f"  {m['name']} | ref={m.get('ref')!r} | state={m.get('state')} | "
          f"partner={p[1] if p else '-'} | tot={m.get('amount_total')} | "
          f"company={co[1] if co else '-'} | origin={m.get('invoice_origin')}")
