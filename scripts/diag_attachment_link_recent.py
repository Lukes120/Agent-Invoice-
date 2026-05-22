"""Diagnosi: ultimi 4 move creati dall'agent → linkati a fatturapa.attachment.in?"""
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

# === STEP A: attachment RAJAPACK 5351870 (certo) ===
print("=" * 80)
print("STEP A — Attachment RAJAPACK 5351870")
print("=" * 80)
att = client._call('fatturapa.attachment.in', 'read', [5351870],
    fields=['id', 'att_name', 'invoices_number', 'invoices_total',
            'registered', 'in_invoice_ids', 'xml_supplier_id',
            'company_id'])[0]
for k, v in att.items():
    print(f"  {k}: {v}")

# in_invoice_ids = move collegati. Se popolato, l'aggancio è avvenuto a livello move.
# registered = True solo se l'agent ha settato il flag.

# === STEP B: cerco il move corrispondente per RAJAPACK ===
print()
print("=" * 80)
print("STEP B — Move RAJAPACK recente (partner 70516, amount 109.43)")
print("=" * 80)
moves = client._call('account.move', 'search_read',
    [('partner_id', '=', 70516),
     ('amount_total', '>=', 109.0), ('amount_total', '<=', 110.0),
     ('move_type', 'in', ['in_invoice', 'in_refund'])],
    fields=['id', 'name', 'state', 'partner_id', 'amount_total',
            'invoice_date', 'create_date', 'fatturapa_attachment_in_id',
            'ref', 'company_id'],
    order='create_date desc',
    limit=5)
for m in moves:
    print(f"  move {m['id']} {m.get('name')} state={m['state']} "
          f"amount={m['amount_total']} ref={m.get('ref')!r}")
    print(f"     fatturapa_attachment_in_id: {m.get('fatturapa_attachment_in_id')}")
    print(f"     create_date: {m.get('create_date')}")

# === STEP C: ultime 10 fatturapa.attachment.in registered=False per company Ecotel ===
print()
print("=" * 80)
print("STEP C — Ultimi 10 attachment registered=False (Ecotel = company 1)")
print("=" * 80)
atts = client._call('fatturapa.attachment.in', 'search_read',
    [('registered', '=', False),
     ('is_self_invoice', '=', False),
     ('company_id', '=', 1)],
    fields=['id', 'att_name', 'invoices_number', 'invoices_total',
            'xml_supplier_id', 'create_date', 'in_invoice_ids'],
    order='create_date desc',
    limit=10)
for a in atts:
    sup = a.get('xml_supplier_id')
    print(f"  att {a['id']} {a.get('att_name','')[:40]!r} "
          f"nf={a.get('invoices_number')} tot={a.get('invoices_total')} "
          f"cedente={sup[1] if sup else '-'}")
    print(f"     create_date={a.get('create_date')} in_invoice_ids={a.get('in_invoice_ids')}")

# === STEP D: ultimi 10 move 'in_invoice'/'in_refund' in stato draft creati di recente ===
print()
print("=" * 80)
print("STEP D — Ultimi 10 move draft (in_invoice/in_refund) Ecotel")
print("=" * 80)
moves = client._call('account.move', 'search_read',
    [('state', '=', 'draft'),
     ('move_type', 'in', ['in_invoice', 'in_refund']),
     ('company_id', '=', 1)],
    fields=['id', 'name', 'partner_id', 'amount_total', 'invoice_date',
            'create_date', 'fatturapa_attachment_in_id', 'ref'],
    order='create_date desc',
    limit=10)
for m in moves:
    p = m.get('partner_id')
    print(f"  move {m['id']} {m.get('name','/'):<10} {p[1] if p else '-':<35} "
          f"amount={m['amount_total']:>10.2f} "
          f"att={m.get('fatturapa_attachment_in_id')}  "
          f"create={m.get('create_date')}")
