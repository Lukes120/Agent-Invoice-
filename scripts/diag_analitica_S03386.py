"""Diagnostica esplorativa: chi popola l'analitica S03386 e su quali conti GL."""
import sys, os
from pathlib import Path
from collections import defaultdict
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')
from core.odoo_client import OdooReadOnlyClient

ANALYTIC_ID = 3813

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'], db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'], password=os.environ['ODOO_PASSWORD'])
client.connect()

# A) account.analytic.line raggruppato per general_account_id
lines = client._call(
    'account.analytic.line', 'search_read',
    [['account_id', '=', ANALYTIC_ID]],
    ['id', 'amount', 'general_account_id', 'product_id', 'employee_id', 'move_id'],
    limit=50000,
)
print(f'Totale account.analytic.line su analitica {ANALYTIC_ID}: {len(lines)}')

by_acc = defaultdict(lambda: {'count': 0, 'amount': 0.0, 'name': None})
no_acc = 0
for l in lines:
    ga = l.get('general_account_id')
    if not ga:
        no_acc += 1
        continue
    aid = ga[0]
    by_acc[aid]['count'] += 1
    by_acc[aid]['amount'] += l.get('amount') or 0
    by_acc[aid]['name'] = ga[1]
print(f'Righe senza general_account_id: {no_acc}')
print(f'GL account distinti: {len(by_acc)}')
for aid, d in sorted(by_acc.items(), key=lambda x: x[1]['amount']):
    print(f"  {aid:>5} | {d['name'][:60]:<60} | {d['count']:>5} righe | {d['amount']:>15,.2f}")

# B) account.move.line con analytic_account_id direttamente
print('\n--- account.move.line.analytic_account_id ---')
amls = client._call(
    'account.move.line', 'search_read',
    [['analytic_account_id', '=', ANALYTIC_ID], ['parent_state', '=', 'posted']],
    ['id', 'debit', 'credit', 'account_id', 'partner_id'],
    limit=20000,
)
print(f'Totale account.move.line con analytic_account_id={ANALYTIC_ID} (posted): {len(amls)}')
by_acc2 = defaultdict(lambda: {'count': 0, 'amount': 0.0, 'name': None})
for l in amls:
    aid = l['account_id'][0]
    by_acc2[aid]['count'] += 1
    by_acc2[aid]['amount'] += (l.get('debit') or 0) - (l.get('credit') or 0)
    by_acc2[aid]['name'] = l['account_id'][1]
for aid, d in sorted(by_acc2.items(), key=lambda x: -x[1]['amount']):
    print(f"  {aid:>5} | {d['name'][:60]:<60} | {d['count']:>5} righe | {d['amount']:>15,.2f}")

# C) Cerca i conti per codice
print('\n--- Conti 430100 / 420180 in DB ---')
for code in ['430100', '420180']:
    accs = client._call('account.account', 'search_read',
                       [['code', '=like', code + '%']], ['id', 'code', 'name'])
    for a in accs:
        print(f"  id={a['id']:>5}  code={a['code']!r}  name={a['name']!r}")
