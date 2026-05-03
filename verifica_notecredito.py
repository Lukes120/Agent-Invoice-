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

# Cerco note credito Trenitalia già registrate (dovrebbero essere move_type='in_refund')
print("=== NOTE CREDITO TRENITALIA (tutti i tipi) ===")
moves = client._call('account.move', 'search_read',
    [('partner_id.name', 'ilike', 'Trenitalia'),
     ('state', '=', 'posted'),
     ('invoice_origin', '=', 'P03524')],
    fields=['id', 'name', 'move_type', 'ref', 'invoice_date',
            'amount_untaxed', 'amount_total'],
    order='invoice_date desc', limit=20)

print(f"\nUltime 20 fatture/NC su P03524:")
print(f"{'Data':<12} {'Tipo':<12} {'Ref':<20} {'Imp':>10} {'Tot':>10}")
for m in moves:
    print(f"{m.get('invoice_date', '?'):<12} "
          f"{m.get('move_type', '?'):<12} "
          f"{(m.get('ref') or '?')[:19]:<20} "
          f"€{m.get('amount_untaxed') or 0:>8.2f} "
          f"€{m.get('amount_total') or 0:>8.2f}")

# Raggruppo per tipo
from collections import Counter
types = Counter(m['move_type'] for m in moves)
print(f"\nDistribuzione tipi: {dict(types)}")