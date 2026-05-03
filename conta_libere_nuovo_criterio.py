import os, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

for oda_name in ['P04279', 'P03524']:
    po = client.search_purchase_order_by_name(oda_name)
    if not po:
        print(f"{oda_name}: non trovato")
        continue

    lines = client._call('purchase.order.line', 'search_read',
        [('order_id', '=', po['id'])],
        fields=['id', 'name', 'price_unit', 'product_qty',
                'qty_invoiced', 'qty_received', 'taxes_id',
                'account_analytic_id'])

    libere_nuovo = [l for l in lines
                    if (l.get('qty_invoiced') or 0) == 0
                    and (l.get('qty_received') or 0) == 0
                    and (l.get('product_qty') or 0) >= 1]

    libere_vecchio = [l for l in lines if (l.get('price_unit') or 0) == 0
                      and (l.get('qty_invoiced') or 0) == 0]

    print(f"\n=== {oda_name} ===")
    print(f"Totale righe: {len(lines)}")
    print(f"Vecchio criterio (price=0 AND inv=0): {len(libere_vecchio)}")
    print(f"NUOVO criterio (inv=0 AND rec=0 AND qty>=1): {len(libere_nuovo)}")

    print(f"\nRighe libere con NUOVO criterio:")
    for l in libere_nuovo:
        analytic = l.get('account_analytic_id')
        analytic_name = analytic[1] if isinstance(analytic, list) else '(vuoto)'
        print(f"  id={l['id']} | '{l.get('name','')}' | "
              f"qty={l.get('product_qty')} | price={l.get('price_unit')} | "
              f"taxes={l.get('taxes_id')} | analitico='{analytic_name}'")