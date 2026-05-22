"""
Parte E: estrai conto contabile + journal + tax dalle 13 fatture posted
WE4SERVICES per allineare la mappatura.
"""
import sys
import os
from collections import Counter
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

# Fatture WE4SERVICES posted collegate a P03696
moves = client._call(
    'account.move', 'search_read',
    [('move_type', '=', 'in_invoice'),
     ('state', '=', 'posted'),
     ('invoice_origin', '=', 'P03696')],
    fields=['id', 'name', 'invoice_date', 'partner_id',
            'journal_id', 'company_id', 'invoice_line_ids',
            'amount_untaxed', 'amount_tax', 'amount_total', 'ref'],
    order='invoice_date desc',
)
print(f"Fatture posted con invoice_origin=P03696: {len(moves)}")

journal_ct = Counter()
account_ct = Counter()
tax_ct = Counter()
journal_names = {}
account_names = {}
tax_names = {}

for m in moves:
    print(f"\n  {m['name']} | {m.get('invoice_date')} | imp={m.get('amount_untaxed')} "
          f"iva={m.get('amount_tax')} tot={m.get('amount_total')} | ref={m.get('ref')}")
    jid = m.get('journal_id')
    if jid:
        journal_ct[jid[0]] += 1
        journal_names[jid[0]] = jid[1]
    line_ids = m.get('invoice_line_ids') or []
    lines = client.get_invoice_lines(line_ids)
    for ln in lines:
        # solo righe non-tax (cioe' quelle del move type 'product')
        # Heuristica: se ha purchase_line_id o account_id, e' una riga utile
        acc = ln.get('account_id')
        if acc:
            account_ct[acc[0]] += 1
            account_names[acc[0]] = acc[1]
        for t in ln.get('tax_ids') or []:
            tax_ct[t] += 1
        print(f"     line: qty={ln.get('quantity')} pu={ln.get('price_unit')} "
              f"subt={ln.get('price_subtotal')} | tax={ln.get('tax_ids')} | "
              f"acct={acc} | po_line={ln.get('purchase_line_id')} | "
              f"name={(ln.get('name') or '')[:60]}")

print("\n" + "=" * 80)
print("RIEPILOGO USO")
print("=" * 80)
print("\nJOURNAL:")
for jid, n in journal_ct.most_common():
    print(f"  id={jid:>3} | n={n} | {journal_names.get(jid)}")

print("\nACCOUNT (per riga, esclude tax_lines):")
for aid, n in account_ct.most_common():
    print(f"  id={aid:>4} | n={n} | {account_names.get(aid)}")

# Risolvi nomi tax
print("\nTAX (frequenza per riga):")
if tax_ct:
    taxes = client._call('account.tax', 'read', list(tax_ct.keys()),
                        fields=['id', 'name', 'amount'])
    for t in taxes:
        tax_names[t['id']] = f"{t['name']} ({t['amount']}%)"
    for tid, n in tax_ct.most_common():
        print(f"  id={tid:>3} | n={n} | {tax_names.get(tid)}")

# Conto contabile del top account
top_acc = account_ct.most_common(1)
if top_acc:
    aid, n = top_acc[0]
    full = client._call('account.account', 'read', [aid],
                       fields=['id', 'code', 'name', 'user_type_id'])
    if full:
        print(f"\n=> Conto contabile dominante: id={aid} code={full[0].get('code')} "
              f"name={full[0].get('name')}")

print("\nDONE.")
