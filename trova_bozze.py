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

url_base = os.getenv('ODOO_URL')

moves = client._call('account.move', 'search_read',
    [('partner_id.name', 'ilike', 'Trenitalia'),
     ('state', '=', 'draft'),
     ('invoice_origin', '=', 'P03524')],
    fields=['id', 'ref', 'invoice_date', 'date', 'amount_untaxed',
            'amount_total', 'invoice_date_due'],
    order='create_date desc', limit=10)

print(f"\nUltime {len(moves)} bozze Trenitalia su P03524:\n")
print(f"{'ID':<7} {'REF':<22} {'DATA FATT':<12} {'DATA COMP':<12} {'SCADENZA':<12} {'IMPONIBILE':>10}")
print("-"*90)
for m in moves:
    print(f"{m['id']:<7} "
          f"{(m.get('ref') or '-')[:21]:<22} "
          f"{m.get('invoice_date','?'):<12} "
          f"{m.get('date','?'):<12} "
          f"{m.get('invoice_date_due','?'):<12} "
          f"€{m.get('amount_untaxed') or 0:>9.2f}")

print(f"\nURL diretti delle bozze:\n")
for m in moves[:5]:
    print(f"  {url_base}/web#id={m['id']}&model=account.move&view_type=form")