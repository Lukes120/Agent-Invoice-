"""
Verifica chi e' "Administrator" e chi ha effettivamente postato le 5 fatture
create da Administrator nel range 20-21/05/2026.
Sola lettura.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

c = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'],
    db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'],
    password=os.environ['ODOO_PASSWORD'],
)
c.connect()

# 1) Chi e' "Administrator"?
print("=" * 60)
print("1. Identita' di 'Administrator'")
print("=" * 60)
users = c._call(
    'res.users', 'search_read',
    [('name', '=', 'Administrator')],
    fields=['id', 'name', 'login', 'email', 'company_id'],
)
for u in users:
    print(f"  id={u['id']}  login={u.get('login')}  email={u.get('email')}  company={u.get('company_id')}")

# 2) Anche res.partner che si chiama Administrator (per author_id dei mail.message)
print()
partners_admin = c._call(
    'res.partner', 'search_read',
    [('name', '=', 'Administrator')],
    fields=['id', 'name', 'email'], limit=10,
)
print(f"  res.partner 'Administrator': {len(partners_admin)} risultati")
for p in partners_admin:
    print(f"    id={p['id']}  email={p.get('email')}")

# 3) Le 5 fatture create da Administrator e state=posted nel range
print()
print("=" * 60)
print("2. Le 5 fatture: chi ha postato? (write_uid e log mail.message)")
print("=" * 60)
fatture = c._call(
    'account.move', 'search_read',
    [
        ('move_type', '=', 'in_invoice'),
        ('create_date', '>=', '2026-05-20 00:00:00'),
        ('create_date', '<', '2026-05-22 00:00:00'),
        ('state', '=', 'posted'),
        ('create_uid.name', '=', 'Administrator'),
    ],
    fields=['id', 'name', 'partner_id', 'amount_total', 'state',
            'create_uid', 'create_date', 'write_uid', 'write_date',
            'invoice_user_id', 'company_id'],
)
print(f"Trovate {len(fatture)} fatture\n")
for f in fatture:
    print(f"  move {f['id']} {f['name']}  partner={f['partner_id'][1] if f.get('partner_id') else None}")
    print(f"     amount_total={f['amount_total']}  company={f.get('company_id')}")
    print(f"     create_uid = {f['create_uid']}  create_date = {f['create_date']}")
    print(f"     write_uid  = {f['write_uid']}   write_date  = {f['write_date']}")
    print(f"     invoice_user_id = {f.get('invoice_user_id')}")

    # mail.message su questo move per vedere chi ha postato (cerco messaggio "Invoice posted" o azione state)
    msgs = c._call(
        'mail.message', 'search_read',
        [('model', '=', 'account.move'), ('res_id', '=', f['id'])],
        fields=['id', 'date', 'author_id', 'subject', 'body', 'subtype_id', 'message_type'],
        order='date asc', limit=20,
    )
    print(f"     mail.message log ({len(msgs)} messaggi):")
    for m in msgs:
        body = (m.get('body') or '')[:150].replace('\n', ' ')
        author = m['author_id'][1] if m.get('author_id') else '<no author>'
        print(f"        {m['date']}  by {author:<25}  type={m.get('message_type')}  body={body!r}")
    print()
