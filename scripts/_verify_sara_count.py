"""
Diagnostica: verifica il count di Sara Piacentini.
Conta tutti gli account.move creati da Sara dal 21/04/2026, raggruppati
per move_type, per capire se ne stiamo escludendo qualcuno.
"""
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, 'config', 'credentials.env'))

from core.odoo_client import OdooReadOnlyClient

cli = OdooReadOnlyClient(
    url=os.getenv('ODOO_URL'),
    db=os.getenv('ODOO_DB'),
    username=os.getenv('ODOO_USERNAME'),
    password=os.getenv('ODOO_PASSWORD'),
)
cli.connect()

# uid Sara
users = cli._call('res.users', 'search_read',
                  [('name', 'ilike', 'Piacentini')],
                  fields=['id', 'name', 'login'], limit=5)
print('Match Piacentini:', users)
sara = users[0]
print(f'Sara uid={sara["id"]} <{sara["login"]}>\n')

CUTOFF = '2026-04-21 00:00:00'

# 1) TUTTI i move creati da Sara dal 21/04, qualsiasi move_type
moves_all = cli._call('account.move', 'search_read',
    [('create_uid', '=', sara['id']), ('create_date', '>=', CUTOFF)],
    fields=['id', 'move_type', 'state', 'create_date'],
    order='create_date asc')
print(f'TOTALE move (qualunque move_type) creati da Sara dal 21/04: {len(moves_all)}')

by_type = Counter(m['move_type'] for m in moves_all)
print('Per move_type:')
for t, n in by_type.most_common():
    print(f'  {t:15s}: {n}')

by_state = Counter(m['state'] for m in moves_all)
print('Per state:')
for s, n in by_state.most_common():
    print(f'  {s:10s}: {n}')

# 2) Solo move passivi (in_invoice + in_refund) — dovrebbe replicare il 211
passivi = [m for m in moves_all if m['move_type'] in ('in_invoice', 'in_refund')]
print(f'\nSolo passivi (in_invoice + in_refund): {len(passivi)}')

# 3) Spaccatura per data di creazione
from collections import defaultdict
by_day = defaultdict(int)
for m in passivi:
    day = (m.get('create_date') or '')[:10]
    by_day[day] += 1
print('\nPassivi per giorno di creazione:')
for d in sorted(by_day):
    print(f'  {d}: {by_day[d]}')

# 4) Primo e ultimo
if moves_all:
    print(f'\nPrimo move (create_date min): {moves_all[0]["create_date"]}')
    print(f'Ultimo move (create_date max): {moves_all[-1]["create_date"]}')

# 5) Check se in active_test=False appare di più (move archiviati)
moves_inactive = cli._call('account.move', 'search_read',
    [('create_uid', '=', sara['id']),
     ('create_date', '>=', CUTOFF),
     '|', ('active', '=', True), ('active', '=', False)],
    fields=['id'])
print(f'\nCon archived inclusi: {len(moves_inactive)}')
