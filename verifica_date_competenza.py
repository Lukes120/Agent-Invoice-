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

# Prendo ultime 10 fatture Trenitalia registrate normalmente (posted)
moves = client._call('account.move', 'search_read',
    [('partner_id.name', 'ilike', 'Trenitalia'),
     ('move_type', 'in', ['in_invoice', 'in_refund']),
     ('state', '=', 'posted'),
     ('invoice_date', '>=', '2026-01-01')],
    fields=['id', 'name', 'ref', 'invoice_date', 'date',
            'invoice_date_due', 'l10n_it_vat_settlement_date',
            'fatturapa_attachment_in_id', 'move_type'],
    order='invoice_date desc', limit=15)

print(f"Trovate {len(moves)} fatture Trenitalia posted nel 2026:\n")
print(f"{'id':<7} {'ref':<22} {'tipo':<11} {'inv_date':<12} {'date':<12} {'scadenza':<12} {'vat_settl':<12}")
print("-"*90)
for m in moves:
    att = m.get('fatturapa_attachment_in_id')
    att_str = f"att={att[0]}" if isinstance(att, list) else "no_att"
    vat_set = m.get('l10n_it_vat_settlement_date') or '-'
    print(f"{m['id']:<7} "
          f"{(m.get('ref') or '-')[:21]:<22} "
          f"{m.get('move_type','?'):<11} "
          f"{m.get('invoice_date','?'):<12} "
          f"{m.get('date','?'):<12} "
          f"{m.get('invoice_date_due','?'):<12} "
          f"{vat_set:<12}")

# Statistica: date == invoice_date o no?
same = sum(1 for m in moves if m.get('date') == m.get('invoice_date'))
diff = len(moves) - same
print(f"\nFatture con date == invoice_date: {same}")
print(f"Fatture con date diverso da invoice_date: {diff}")