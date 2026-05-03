import os, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient
from datetime import date, timedelta
from collections import Counter

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

# Analisi stati ultimi 30 giorni
date_from = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
date_to = date.today().strftime('%Y-%m-%d')

# Distribuzione per stato
all_bills = client._call('account.move', 'search_read',
    [('move_type', '=', 'in_invoice'),
     ('invoice_date', '>=', date_from),
     ('invoice_date', '<=', date_to)],
    fields=['id', 'name', 'state', 'invoice_date', 'invoice_origin',
            'partner_id', 'amount_total'])

print(f"Totale fatture fornitore ultimi 30gg: {len(all_bills)}")
print()

# Conteggio per stato
states = Counter(b['state'] for b in all_bills)
print("Distribuzione per stato:")
for s, c in states.most_common():
    print(f"  {s}: {c}")
print()

# Quante hanno un invoice_origin (OdA collegato)
with_origin = [b for b in all_bills if b.get('invoice_origin')]
without_origin = [b for b in all_bills if not b.get('invoice_origin')]
print(f"Con invoice_origin (OdA collegato): {len(with_origin)}")
print(f"Senza invoice_origin: {len(without_origin)}")
print()

# Esempi invoice_origin per capire il formato
print("Primi 10 invoice_origin (per capire formato OdA):")
for b in with_origin[:10]:
    print(f"  {b.get('name')}: origin='{b.get('invoice_origin')}'")

# Cerco campi specifici italiani
print("\nCerco campi fatturazione elettronica italiana...")
try:
    fields_info = client._call('account.move', 'fields_get', [],
        attributes=['string', 'type'])
    italian_fields = {k: v for k, v in fields_info.items()
                     if 'l10n_it' in k.lower() or 'edi' in k.lower()
                     or 'sdi' in k.lower() or 'fattura' in k.lower()}
    print(f"Campi italiani/EDI trovati: {len(italian_fields)}")
    for k, v in list(italian_fields.items())[:15]:
        print(f"  {k}: {v.get('string')} ({v.get('type')})")
except Exception as e:
    print(f"Errore: {e}")