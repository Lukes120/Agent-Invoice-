"""Trova i 4 move draft creati oggi 16/05 NON linkati a fatturapa.attachment.in.
Per ognuno trova l'attachment corrispondente in 'e-fatture in ingresso' registered=False.
Evita di includere fatturapa_attachment_in_id nei fields per non innescare cascade
display_name che crasha (column tipo_documento does not exist)."""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'], db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'], password=os.environ['ODOO_PASSWORD'])
client.connect()

# Step 1: tutti i move draft Ecotel creati dal 16/05 in poi
moves = client._call('account.move', 'search_read',
    [('state', '=', 'draft'),
     ('move_type', 'in', ['in_invoice', 'in_refund']),
     ('company_id', '=', 1),
     ('create_date', '>=', '2026-05-16 00:00:00')],
    fields=['id', 'name', 'partner_id', 'amount_total', 'amount_untaxed',
            'invoice_date', 'create_date', 'ref', 'move_type'],
    order='create_date desc',
    limit=50)

print("=" * 100)
print(f"MOVE DRAFT Ecotel creati dal 2026-05-16 ({len(moves)} totali)")
print("=" * 100)
for m in moves:
    p = m.get('partner_id')
    print(f"  move {m['id']:>6}  {m.get('name','/'):<10}  {(p[1] if p else '-'):<35}  "
          f"tot={m['amount_total']:>10.2f}  ref={m.get('ref') or '-':<25}  "
          f"date={m.get('invoice_date')}  create={m.get('create_date')}  type={m.get('move_type')}")

# Step 2: per ognuno verifico se è linkato a un attachment
# Lo faccio chiedendo il campo fatturapa_attachment_in_id UNO ALLA VOLTA in modo da
# isolare il crash (e capire quali sono linkati e quali no).
print()
print("=" * 100)
print("LINK STATUS per ogni move (campo fatturapa_attachment_in_id, query singola)")
print("=" * 100)
for m in moves:
    try:
        rec = client._call('account.move', 'read', [m['id']],
                           fields=['id', 'fatturapa_attachment_in_id'])[0]
        att_link = rec.get('fatturapa_attachment_in_id')
        if att_link:
            print(f"  move {m['id']:>6}  LINKED to att {att_link[0]} ({att_link[1][:50]})")
        else:
            print(f"  move {m['id']:>6}  *** NON LINKED ***  ({(m.get('partner_id') or ['','-'])[1][:30]} "
                  f"€{m['amount_total']:.2f} ref={m.get('ref')!r})")
    except Exception as e:
        print(f"  move {m['id']:>6}  ERROR read fatturapa_attachment_in_id: {str(e)[:120]}")

# Step 3: per ogni move non linkato, cerco l'attachment match (partner + amount + ref)
print()
print("=" * 100)
print("MATCH move <-> attachment per i NON LINKED (partner + amount + invoices_number)")
print("=" * 100)
for m in moves:
    rec = client._call('account.move', 'read', [m['id']],
                       fields=['fatturapa_attachment_in_id'])[0]
    if rec.get('fatturapa_attachment_in_id'):
        continue
    p = m.get('partner_id')
    if not p:
        continue
    partner_id = p[0]
    amount = m['amount_total']
    ref = m.get('ref') or ''
    # Cerco attachment registered=False stesso cedente + amount
    cands = client._call('fatturapa.attachment.in', 'search_read',
        [('registered', '=', False),
         ('is_self_invoice', '=', False),
         ('company_id', '=', 1),
         ('xml_supplier_id', '=', partner_id),
         ('invoices_total', '>=', amount - 0.01),
         ('invoices_total', '<=', amount + 0.01)],
        fields=['id', 'att_name', 'invoices_number', 'invoices_total',
                'invoices_date', 'create_date'],
        order='create_date desc',
        limit=5)
    print(f"  move {m['id']} ({(p[1] or '')[:30]} €{amount:.2f} ref={ref!r}):")
    for c in cands:
        match_ref = (c.get('invoices_number') or '').strip() == ref.strip()
        flag = '*' if match_ref else ' '
        print(f"     {flag} att {c['id']}  nf={c.get('invoices_number')!r}  "
              f"tot={c['invoices_total']}  data={c.get('invoices_date')}")
