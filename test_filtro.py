import os, sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

# Prendo tutti i record non registrati con molti campi per diagnosi
records = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('registered', '=', False)],
    fields=['id', 'registered', 'e_invoice_validation_error',
            'e_invoice_parsing_error', 'in_invoice_ids',
            'is_self_invoice', 'invoices_number',
            'company_id', 'xml_supplier_id', 'create_date']
)

print(f"Totale con registered=False: {len(records)}\n")

# Distribuzioni
has_validation_err = [r for r in records if r.get('e_invoice_validation_error')]
has_parsing_err = [r for r in records if r.get('e_invoice_parsing_error')]
has_invoice = [r for r in records if r.get('in_invoice_ids')]
is_self = [r for r in records if r.get('is_self_invoice')]
zero_invoices = [r for r in records if not r.get('invoices_number')]

print(f"Con errore validazione:        {len(has_validation_err)}")
print(f"Con errore parsing:            {len(has_parsing_err)}")
print(f"Con in_invoice_ids popolato:   {len(has_invoice)}")
print(f"Self-invoice:                  {len(is_self)}")
print(f"Con invoices_number = 0:       {len(zero_invoices)}")
print()

# Distribuzione per company
companies = Counter(r['company_id'][1] if r.get('company_id') else 'None' for r in records)
print("Distribuzione per azienda:")
for c, n in companies.most_common():
    print(f"  {c}: {n}")
print()

# Distribuzione temporale
from datetime import datetime
dates = Counter()
for r in records:
    cd = r.get('create_date', '')
    if cd:
        month = cd[:7]  # YYYY-MM
        dates[month] += 1
print("Distribuzione per mese di ricezione:")
for m, n in sorted(dates.items(), reverse=True)[:12]:
    print(f"  {m}: {n}")
print()

# Test combinazioni di filtri per trovare quello che dà 212
print("=" * 60)
print("Test combinazioni filtri:")
print("=" * 60)
tests = [
    ("registered=False", [('registered', '=', False)]),
    ("+ nessun errore validazione",
     [('registered', '=', False), ('e_invoice_validation_error', '=', False)]),
    ("+ company_id=1",
     [('registered', '=', False), ('e_invoice_validation_error', '=', False), ('company_id', '=', 1)]),
    ("+ non self_invoice",
     [('registered', '=', False), ('e_invoice_validation_error', '=', False),
      ('is_self_invoice', '=', False)]),
    ("+ senza invoice collegata (in_invoice_ids vuoto)",
     [('registered', '=', False), ('in_invoice_ids', '=', False)]),
    ("+ invoices_number > 0",
     [('registered', '=', False), ('invoices_number', '>', 0)]),
]
for label, domain in tests:
    try:
        count = client._call('fatturapa.attachment.in', 'search_count', domain)
        print(f"  {label}: {count}")
    except Exception as e:
        print(f"  {label}: errore - {e}")