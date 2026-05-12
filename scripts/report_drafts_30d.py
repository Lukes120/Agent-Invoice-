"""
Report: bozze create dall'agent (utente Luca Ranalletta) negli ultimi 30 giorni
- Quante sono fatture (TD01/TD24/TD25) vs note credito (TD04)
- Quante sono ancora draft vs passate a posted
- Per quelle posted: chi le ha posted (write_uid)

Read-only. Non modifica nulla.
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta
from collections import Counter, defaultdict

# percorso radice progetto
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, 'config', 'credentials.env'))

from core.odoo_client import OdooReadOnlyClient

DB_PATH = os.path.join(ROOT, 'webapp', 'dashboard.db')


def main():
    cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
    print(f'Cutoff (ultimi 30gg): {cutoff}')
    print('-' * 70)

    # 1) Schema odoo_writes
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(odoo_writes)")
    cols = [r[1] for r in cur.fetchall()]
    print('Colonne odoo_writes:', cols)

    # 2) Estraggo i move_id creati negli ultimi 30 giorni, NON rollbackati
    #    Filtro: timestamp >= cutoff e (rolled_back NULL o False) se la colonna esiste
    has_rollback = 'rolled_back' in cols
    has_ts = 'timestamp' in cols
    has_created = 'created_at' in cols
    ts_col = 'timestamp' if has_ts else ('created_at' if has_created else None)
    print(f'Colonna timestamp: {ts_col}, rollback flag: {has_rollback}')

    # provo a vedere prima 1 riga
    cur.execute("SELECT * FROM odoo_writes ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        print('Esempio riga:', dict(zip(cols, row)))
    else:
        print('Tabella odoo_writes vuota')
        return

    # query move_id
    where = []
    params = []
    if ts_col:
        where.append(f'{ts_col} >= ?')
        params.append(cutoff)
    if has_rollback:
        where.append('(rolled_back IS NULL OR rolled_back = 0)')
    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''

    sql = f'SELECT DISTINCT move_id FROM odoo_writes{where_sql}'
    cur.execute(sql, params)
    move_ids = [r[0] for r in cur.fetchall() if r[0]]
    print(f'\nMove_id distinti scritti dall agent negli ultimi 30gg: {len(move_ids)}')

    # arricchisco con tipo_documento e analysis_id se presenti
    move_meta = {}
    if 'analysis_id' in cols:
        sql2 = f"""SELECT ow.move_id, a.tipo_documento, a.numero_fattura, a.partner_nome,
                          ow.{ts_col if ts_col else 'rowid'} as ts
                   FROM odoo_writes ow
                   LEFT JOIN analyses a ON a.id = ow.analysis_id
                   {where_sql}"""
        cur.execute(sql2, params)
        for mid, td, num, partn, ts in cur.fetchall():
            if mid and mid not in move_meta:
                move_meta[mid] = {'td': td, 'num': num, 'partner': partn, 'ts': ts}

    conn.close()

    if not move_ids:
        print('Nessun move da analizzare.')
        return

    # 3) Connetto a Odoo e leggo lo stato attuale
    print('\nConnessione a Odoo...')
    cli = OdooReadOnlyClient(
        url=os.getenv('ODOO_URL'),
        db=os.getenv('ODOO_DB'),
        username=os.getenv('ODOO_USERNAME'),
        password=os.getenv('ODOO_PASSWORD'),
    )
    cli.connect()
    print(f'Connesso. Mio uid={cli.uid}')

    # leggo i move
    fields = ['id', 'name', 'state', 'move_type', 'invoice_date', 'date',
              'amount_total', 'partner_id', 'create_uid', 'write_uid',
              'create_date', 'write_date']
    moves = cli._call('account.move', 'read', move_ids, fields=fields)
    print(f'Letti {len(moves)} record da Odoo (di {len(move_ids)} richiesti)\n')

    # diff: id mancanti = già unlinkati
    found_ids = {m['id'] for m in moves}
    missing = [mid for mid in move_ids if mid not in found_ids]
    if missing:
        print(f'Move non più presenti in Odoo (unlinkati?): {len(missing)}')
        print(f'  IDs: {missing[:20]}{"..." if len(missing)>20 else ""}\n')

    # 4) Aggregazione
    by_state = Counter()
    by_state_td = defaultdict(Counter)
    posted_by_user = Counter()
    posted_users = set()

    detail_rows = []

    for m in moves:
        mid = m['id']
        meta = move_meta.get(mid, {})
        td = meta.get('td') or '?'
        # se TD non disponibile, deduco da move_type
        if td == '?':
            td = 'TD04' if m.get('move_type') == 'in_refund' else 'TD01'
        state = m.get('state')
        write_uid = m.get('write_uid')
        write_uid_id = write_uid[0] if write_uid else None
        write_uid_name = write_uid[1] if write_uid else '?'

        by_state[state] += 1
        by_state_td[td][state] += 1

        if state == 'posted':
            posted_users.add((write_uid_id, write_uid_name))
            posted_by_user[write_uid_name] += 1

        detail_rows.append({
            'id': mid,
            'name': m.get('name'),
            'td': td,
            'move_type': m.get('move_type'),
            'state': state,
            'partner': (m.get('partner_id') or [None, ''])[1],
            'amount': m.get('amount_total'),
            'invoice_date': m.get('invoice_date'),
            'write_uid': write_uid_name,
            'write_date': m.get('write_date'),
        })

    # 5) Output
    print('=' * 70)
    print('RIEPILOGO COMPLESSIVO')
    print('=' * 70)
    print(f'Bozze totali create dall agent (utenza Luca Ranalletta) ultimi 30gg: {len(move_ids)}')
    print(f'  - ancora trovate in Odoo: {len(moves)}')
    print(f'  - non più presenti (cancellate/unlinkate): {len(missing)}')
    print()
    print('Stato attuale (solo quelle ancora in Odoo):')
    for st, cnt in by_state.most_common():
        print(f'  {st:12s}: {cnt}')
    print()
    print('Per tipo documento:')
    for td in sorted(by_state_td.keys()):
        sub = by_state_td[td]
        tot = sum(sub.values())
        post = sub.get('posted', 0)
        draft = sub.get('draft', 0)
        cancel = sub.get('cancel', 0)
        print(f'  {td:6s} totale={tot:4d}  draft={draft:4d}  posted={post:4d}  cancel={cancel:4d}')
    print()
    print('Posted: chi ha confermato (write_uid):')
    for user, cnt in posted_by_user.most_common():
        print(f'  {user:40s}: {cnt}')

    # 6) Salvo CSV di dettaglio
    out_csv = os.path.join(ROOT, 'output', 'report_drafts_30d.csv')
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    import csv
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        if detail_rows:
            w = csv.DictWriter(f, fieldnames=list(detail_rows[0].keys()))
            w.writeheader()
            w.writerows(detail_rows)
    print(f'\nDettaglio salvato in: {out_csv}')


if __name__ == '__main__':
    main()
