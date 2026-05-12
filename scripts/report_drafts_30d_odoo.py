"""
Report REALTIME su Odoo: bozze create dall'utenza Luca Ranalletta negli
ultimi 30 giorni e loro stato attuale (draft/posted/cancel + chi ha posted).

Filtri per isolare l'agent: solo move con un fatturapa_attachment_in
collegato (pattern dell'agent) e/o invoice_origin valorizzato.

Read-only: usa OdooReadOnlyClient.
"""
import os
import sys
from datetime import datetime, timedelta
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, 'config', 'credentials.env'))

from core.odoo_client import OdooReadOnlyClient


def main():
    cli = OdooReadOnlyClient(
        url=os.getenv('ODOO_URL'),
        db=os.getenv('ODOO_DB'),
        username=os.getenv('ODOO_USERNAME'),
        password=os.getenv('ODOO_PASSWORD'),
    )
    cli.connect()
    print(f'Connesso. Mio uid={cli.uid}')

    # 1) Risolvo uid Luca dal login (oppure dal nome)
    login = os.getenv('ODOO_USERNAME')
    users = cli._call('res.users', 'search_read',
                      [('login', '=', login)],
                      fields=['id', 'name', 'login'], limit=1)
    if not users:
        # fallback: cerco per nome
        users = cli._call('res.users', 'search_read',
                          [('name', 'ilike', 'Ranalletta')],
                          fields=['id', 'name', 'login'], limit=1)
    if not users:
        print('Utente Luca non trovato')
        return
    luca = users[0]
    print(f'Utente target: {luca["name"]} <{luca["login"]}> uid={luca["id"]}')

    # 2) Verifico campo fatturapa_attachment_in_id su account.move
    fields_info = cli._call('account.move', 'fields_get', [], attributes=['string', 'type'])
    fatturapa_field = None
    for f in ['fatturapa_attachment_in_id', 'l10n_it_einvoice_id']:
        if f in fields_info:
            fatturapa_field = f
            break
    print(f'Campo fatturaPA disponibile: {fatturapa_field}')

    # 3) Domain: tutte le bozze/registrazioni create da Luca negli ultimi 30 giorni
    cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
    print(f'Cutoff create_date: >= {cutoff}\n')

    domain = [
        ('create_uid', '=', luca['id']),
        ('create_date', '>=', cutoff),
        ('move_type', 'in', ['in_invoice', 'in_refund']),
    ]
    fields = ['id', 'name', 'state', 'move_type', 'invoice_date', 'date',
              'amount_total', 'partner_id', 'invoice_origin',
              'create_uid', 'create_date', 'write_uid', 'write_date']
    if fatturapa_field:
        fields.append(fatturapa_field)

    moves = cli._call('account.move', 'search_read', domain,
                      fields=fields, order='create_date asc')
    print(f'Move totali create da Luca (in_invoice/in_refund) ultimi 30gg: {len(moves)}')

    # 4) Filtro "agent": ha fatturapa_attachment_in_id valorizzato
    if fatturapa_field:
        agent_moves = [m for m in moves if m.get(fatturapa_field)]
        manual_moves = [m for m in moves if not m.get(fatturapa_field)]
    else:
        # fallback: invoice_origin che inizia per P (OdA)
        agent_moves = [m for m in moves if (m.get('invoice_origin') or '').startswith('P')]
        manual_moves = [m for m in moves if not (m.get('invoice_origin') or '').startswith('P')]

    print(f'  - con fatturaPA collegata (= agent): {len(agent_moves)}')
    print(f'  - senza fatturaPA (manuali / altri flussi): {len(manual_moves)}')
    print()

    # 5) Aggregazione su agent_moves
    by_state = Counter()
    by_td_state = defaultdict(Counter)
    posted_by_user = Counter()
    sample_posted = []
    sample_draft = []

    for m in agent_moves:
        td = 'TD04' if m.get('move_type') == 'in_refund' else 'TD01-like'
        st = m.get('state')
        by_state[st] += 1
        by_td_state[td][st] += 1
        if st == 'posted':
            wu = m.get('write_uid')
            wu_name = wu[1] if wu else '?'
            posted_by_user[wu_name] += 1
            if len(sample_posted) < 8:
                sample_posted.append(m)
        elif st == 'draft':
            if len(sample_draft) < 5:
                sample_draft.append(m)

    print('=' * 72)
    print(f'BOZZE CREATE DALL AGENT (utenza {luca["name"]}) - ULTIMI 30 GIORNI')
    print('=' * 72)
    print(f'Totale: {len(agent_moves)}')
    print()
    print('Stato attuale:')
    for st, cnt in by_state.most_common():
        pct = 100.0 * cnt / max(1, len(agent_moves))
        print(f'  {st:10s}: {cnt:4d}  ({pct:5.1f}%)')
    print()
    print('Per tipo documento (TD01-like = in_invoice, TD04 = in_refund):')
    for td in sorted(by_td_state.keys()):
        sub = by_td_state[td]
        tot = sum(sub.values())
        d = sub.get('draft', 0)
        p = sub.get('posted', 0)
        c = sub.get('cancel', 0)
        print(f'  {td:12s}  totale={tot:4d}  draft={d:4d}  posted={p:4d}  cancel={c:4d}')
    print()
    print('-' * 72)
    print('CHI HA POSTED (write_uid sui posted):')
    print('-' * 72)
    if posted_by_user:
        tot_posted = sum(posted_by_user.values())
        for u, cnt in posted_by_user.most_common():
            pct = 100.0 * cnt / tot_posted
            print(f'  {u:45s}: {cnt:4d}  ({pct:5.1f}%)')
    else:
        print('  Nessuna bozza ancora posted')
    print()

    # 6) Sample
    print('-' * 72)
    print('Esempi posted:')
    for m in sample_posted:
        wu = (m.get('write_uid') or [None, '?'])[1]
        partner = (m.get('partner_id') or [None, ''])[1]
        print(f'  [{m["id"]}] {m.get("name") or "(no name)":20s}  '
              f'{m.get("move_type"):10s}  {partner[:35]:35s}  '
              f'tot={m.get("amount_total")}  -> posted da {wu} il {m.get("write_date")}')
    if sample_draft:
        print()
        print('Esempi ancora in draft:')
        for m in sample_draft:
            partner = (m.get('partner_id') or [None, ''])[1]
            print(f'  [{m["id"]}] {m.get("invoice_origin") or "":8s}  '
                  f'{m.get("move_type"):10s}  {partner[:35]:35s}  '
                  f'tot={m.get("amount_total")}  creato il {m.get("create_date")}')


if __name__ == '__main__':
    main()
