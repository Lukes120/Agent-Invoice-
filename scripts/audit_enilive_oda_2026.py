"""
Audit READ-ONLY: stato OdA Enilive 2026 e residui sui ledger noti P03731/P03764.

Output:
- Partner Enilive (P.IVA IT11403240960) → id, nome, eventuali duplicati
- Tutti i purchase.order Enilive aperti (state=purchase, qualsiasi anno) → name, date_order, amount_total, qty_invoiced vs product_qty
- Verifica esplicita P03731 e P03764: POL totali, POL libere (qty_invoiced=0 & qty_received=0 & product_qty>=1)
- Eventuali nuovi OdA 2026 (date_order >= 2026-01-01)
"""

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

PARTITA_IVA_ENILIVE = 'IT11403240960'
ODA_LEDGERS_NOTI = ['P03731', 'P03764']


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    print('=' * 70)
    print('AUDIT ENILIVE — OdA 2026 e residui ledger noti')
    print('=' * 70)

    partners = client._call(
        'res.partner', 'search_read',
        [['vat', '=', PARTITA_IVA_ENILIVE]],
        ['id', 'name', 'vat', 'company_id', 'is_company', 'active'],
    )
    print(f'\n[PARTNER] match su P.IVA {PARTITA_IVA_ENILIVE}: {len(partners)}')
    for p in partners:
        print(f'  - id={p["id"]:>6}  name={p["name"]!r}  company={p.get("company_id")}  active={p["active"]}')

    if not partners:
        print('\n⚠  Nessun partner trovato. Stop.')
        return

    partner_ids = [p['id'] for p in partners]

    print('\n' + '=' * 70)
    print('PURCHASE.ORDER Enilive (TUTTI gli stati, ordinati per data_order desc)')
    print('=' * 70)
    pos = client._call(
        'purchase.order', 'search_read',
        [['partner_id', 'in', partner_ids]],
        ['id', 'name', 'date_order', 'state', 'amount_total', 'amount_untaxed',
         'company_id', 'currency_id', 'origin', 'partner_ref'],
        order='date_order desc',
    )
    print(f'\nTotale PO trovati: {len(pos)}\n')
    print(f'{"NAME":<10} {"STATE":<10} {"DATE":<12} {"TOTAL":>12} {"COMPANY":<8} {"ORIGIN":<25}')
    print('-' * 90)
    for po in pos:
        company = po['company_id'][1] if po.get('company_id') else ''
        print(f'{po["name"]:<10} {po["state"]:<10} {str(po["date_order"])[:10]:<12} '
              f'{po["amount_total"]:>12,.2f} {company[:8]:<8} {(po.get("origin") or "")[:25]:<25}')

    print('\n' + '=' * 70)
    print('FOCUS: ledger noti P03731 / P03764')
    print('=' * 70)
    for oda_name in ODA_LEDGERS_NOTI:
        po_match = [p for p in pos if p['name'] == oda_name]
        if not po_match:
            print(f'\n  ⚠  {oda_name}: NON trovato fra i PO Enilive')
            continue
        po = po_match[0]
        print(f'\n--- {oda_name} (id={po["id"]}, state={po["state"]}, total={po["amount_total"]:,.2f}) ---')
        pols = client._call(
            'purchase.order.line', 'search_read',
            [['order_id', '=', po['id']]],
            ['id', 'name', 'product_qty', 'qty_invoiced', 'qty_received',
             'price_unit', 'price_subtotal', 'taxes_id', 'product_id', 'date_planned'],
            order='id asc',
        )
        libere = [l for l in pols
                  if l['qty_invoiced'] == 0
                  and l['qty_received'] == 0
                  and l['product_qty'] >= 1]
        usate = [l for l in pols if l['qty_invoiced'] > 0 or l['qty_received'] > 0]
        print(f'    POL totali: {len(pols)}  |  libere (qi=0,qr=0,qty>=1): {len(libere)}  |  usate: {len(usate)}')
        if libere:
            print(f'    Prime 5 libere:')
            for l in libere[:5]:
                print(f'      id={l["id"]:>7}  qty={l["product_qty"]:>5}  price={l["price_unit"]:>9.2f}  '
                      f'name={l["name"][:40]!r}')

    print('\n' + '=' * 70)
    print('NUOVI OdA 2026 (date_order >= 2026-01-01)')
    print('=' * 70)
    pos_2026 = [p for p in pos if str(p['date_order'])[:10] >= '2026-01-01']
    if not pos_2026:
        print('\n  ⚠  Nessun OdA Enilive 2026 trovato.')
    else:
        print(f'\n  ✓ Trovati {len(pos_2026)} OdA 2026:')
        for po in pos_2026:
            company = po['company_id'][1] if po.get('company_id') else ''
            print(f'    {po["name"]:<10}  state={po["state"]:<10}  date={str(po["date_order"])[:10]}  '
                  f'total={po["amount_total"]:>12,.2f}  company={company}')

    print('\n' + '=' * 70)
    print('FINE AUDIT')
    print('=' * 70)


if __name__ == '__main__':
    main()
