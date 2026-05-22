"""
Conta fatture fornitore per utente (create/post) negli ultimi 2 giorni (20-21/05/2026).
Sola lettura via OdooReadOnlyClient.

- "In posta" / bozze: account.move con move_type='in_invoice' e create_date nel range,
  raggruppato per create_uid.
- "Confermate": account.move con move_type='in_invoice', state='posted' e write_date
  nel range, raggruppato per write_uid.
"""
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
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

# Date range: ieri 20/05 e oggi 21/05 (con timezone server). Uso 2 giorni indietro per sicurezza.
# In Odoo create_date / write_date sono in UTC. Filtro su finestre giornaliere larghe.
today = datetime(2026, 5, 21)
ieri = datetime(2026, 5, 20)
inizio = ieri.strftime('%Y-%m-%d 00:00:00')
fine = (today + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')

print(f"Range: {inizio} -> {fine} (UTC)")
print()

# ------------------------------------------------------------
# A. BOZZE create nel range (qualsiasi stato attuale, ma create_date in range)
# ------------------------------------------------------------
print("=" * 60)
print("A. FATTURE CREATE (bozze 'messe in posta') per create_uid")
print("=" * 60)

created = client._call(
    'account.move', 'search_read',
    [
        ('move_type', '=', 'in_invoice'),
        ('create_date', '>=', inizio),
        ('create_date', '<', fine),
    ],
    fields=['id', 'name', 'state', 'create_uid', 'create_date', 'company_id'],
    limit=5000,
)

by_user_created = defaultdict(lambda: Counter())
by_user_created_giorno = defaultdict(lambda: Counter())
for m in created:
    uid_name = m['create_uid'][1] if m.get('create_uid') else '<no uid>'
    state = m['state']
    by_user_created[uid_name][state] += 1
    giorno = m['create_date'][:10]
    by_user_created_giorno[(uid_name, giorno)][state] += 1

print(f"Totale fatture create nel range: {len(created)}")
print()
print(f"{'Utente':<40} {'draft':>8} {'posted':>8} {'cancel':>8} {'totale':>8}")
print("-" * 80)
for utente in sorted(by_user_created.keys()):
    stati = by_user_created[utente]
    tot = sum(stati.values())
    print(f"{utente:<40} {stati.get('draft', 0):>8} {stati.get('posted', 0):>8} "
          f"{stati.get('cancel', 0):>8} {tot:>8}")

print()
print("Per giorno:")
for (utente, giorno), stati in sorted(by_user_created_giorno.items()):
    tot = sum(stati.values())
    print(f"  {giorno}  {utente:<35} draft={stati.get('draft',0)} posted={stati.get('posted',0)} cancel={stati.get('cancel',0)} tot={tot}")

# ------------------------------------------------------------
# B. POSTED nel range (write_uid + write_date)
# ------------------------------------------------------------
print()
print("=" * 60)
print("B. FATTURE POSTED (write_date in range, state='posted') per write_uid")
print("   Nota: write_uid riflette l'ULTIMA modifica, può non essere chi ha postato")
print("=" * 60)

posted = client._call(
    'account.move', 'search_read',
    [
        ('move_type', '=', 'in_invoice'),
        ('state', '=', 'posted'),
        ('write_date', '>=', inizio),
        ('write_date', '<', fine),
    ],
    fields=['id', 'name', 'state', 'write_uid', 'write_date', 'create_uid', 'create_date'],
    limit=5000,
)

by_user_posted = Counter()
by_user_posted_giorno = defaultdict(Counter)
for m in posted:
    uid_name = m['write_uid'][1] if m.get('write_uid') else '<no uid>'
    by_user_posted[uid_name] += 1
    giorno = m['write_date'][:10]
    by_user_posted_giorno[giorno][uid_name] += 1

print(f"Totale fatture posted con write_date nel range: {len(posted)}")
print()
print(f"{'Utente (write_uid)':<40} {'count':>8}")
print("-" * 50)
for utente, n in by_user_posted.most_common():
    print(f"{utente:<40} {n:>8}")

print()
print("Per giorno:")
for giorno in sorted(by_user_posted_giorno.keys()):
    print(f"  {giorno}:")
    for utente, n in by_user_posted_giorno[giorno].most_common():
        print(f"     {utente:<40} {n:>4}")

# ------------------------------------------------------------
# C. Più preciso: chi ha postato (cerco mail.message con tracciamento state)
# ------------------------------------------------------------
print()
print("=" * 60)
print("C. CHI HA POSTATO (mail.tracking.value su account.move, campo 'state')")
print("=" * 60)

# 1) trova mail.tracking.value relative al campo state cambiato a 'Posted' nelle date
# Field: state, modello account.move. ir.model.fields id si recupera.
field_id = client._call(
    'ir.model.fields', 'search_read',
    [('model', '=', 'account.move'), ('name', '=', 'state')],
    fields=['id'], limit=1,
)
if not field_id:
    print("Campo state non trovato su account.move (ir.model.fields)")
else:
    fid = field_id[0]['id']
    trackings = client._call(
        'mail.tracking.value', 'search_read',
        [
            ('field', '=', fid),
            ('create_date', '>=', inizio),
            ('create_date', '<', fine),
            ('new_value_char', '=', 'Posted'),
        ],
        fields=['id', 'create_uid', 'create_date', 'mail_message_id'],
        limit=5000,
    )
    print(f"mail.tracking.value (state -> Posted) trovati: {len(trackings)}")

    # Per ognuno risalgo a mail.message per capire il res_id e res_model
    msg_ids = list({t['mail_message_id'][0] for t in trackings if t.get('mail_message_id')})
    messages = {}
    if msg_ids:
        msgs = client._call(
            'mail.message', 'read', msg_ids,
            fields=['id', 'model', 'res_id', 'author_id', 'date'],
        )
        messages = {m['id']: m for m in msgs}

    by_author_posting = Counter()
    by_author_posting_giorno = defaultdict(Counter)
    move_ids_seen = set()
    rows_invalid = 0
    for t in trackings:
        mid = t.get('mail_message_id')
        if not mid:
            continue
        msg = messages.get(mid[0])
        if not msg or msg.get('model') != 'account.move':
            rows_invalid += 1
            continue
        move_ids_seen.add(msg['res_id'])
        author = msg['author_id'][1] if msg.get('author_id') else '<no author>'
        by_author_posting[author] += 1
        giorno = (msg.get('date') or t['create_date'])[:10]
        by_author_posting_giorno[giorno][author] += 1

    # Filtra solo i move_type='in_invoice'
    if move_ids_seen:
        moves = client._call(
            'account.move', 'read', list(move_ids_seen),
            fields=['id', 'move_type', 'name'],
        )
        in_invoice_ids = {m['id'] for m in moves if m.get('move_type') == 'in_invoice'}
    else:
        in_invoice_ids = set()

    # Ricompongo solo per fatture fornitore
    by_author_posting_inv = Counter()
    by_author_posting_inv_giorno = defaultdict(Counter)
    for t in trackings:
        mid = t.get('mail_message_id')
        if not mid:
            continue
        msg = messages.get(mid[0])
        if not msg or msg.get('model') != 'account.move':
            continue
        if msg['res_id'] not in in_invoice_ids:
            continue
        author = msg['author_id'][1] if msg.get('author_id') else '<no author>'
        by_author_posting_inv[author] += 1
        giorno = (msg.get('date') or t['create_date'])[:10]
        by_author_posting_inv_giorno[giorno][author] += 1

    print(f"  di cui su account.move (fatture in_invoice): {sum(by_author_posting_inv.values())}")
    print()
    print(f"{'Utente che ha POSTATO':<40} {'count':>8}")
    print("-" * 50)
    for utente, n in by_author_posting_inv.most_common():
        print(f"{utente:<40} {n:>8}")

    print()
    print("Per giorno (POSTING):")
    for giorno in sorted(by_author_posting_inv_giorno.keys()):
        print(f"  {giorno}:")
        for utente, n in by_author_posting_inv_giorno[giorno].most_common():
            print(f"     {utente:<40} {n:>4}")
