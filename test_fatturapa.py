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

print("=" * 60)
print("CAMPI DEL MODELLO fatturapa.attachment.in")
print("=" * 60)

fields = client._call('fatturapa.attachment.in', 'fields_get', [],
    attributes=['string', 'type'])
for name, info in sorted(fields.items()):
    print(f"  {name}: {info.get('string')} ({info.get('type')})")

print()
print("=" * 60)
print("CONTEGGIO RECORD 'DA REGISTRARE'")
print("=" * 60)

# Provo diversi filtri plausibili
for domain_desc, domain in [
    ("registered = False", [('registered', '=', False)]),
    ("tutti i record", []),
]:
    try:
        count = client._call('fatturapa.attachment.in', 'search_count', domain)
        print(f"  {domain_desc}: {count} record")
    except Exception as e:
        print(f"  {domain_desc}: errore - {e}")

print()
print("=" * 60)
print("RECORD DI ESEMPIO CON TUTTI I DATI")
print("=" * 60)
sample = client._call('fatturapa.attachment.in', 'search_read',
    [], fields=list(fields.keys()), limit=1, order='id desc')
if sample:
    rec = sample[0]
    for k in sorted(rec.keys()):
        v = rec[k]
        # Tronco valori lunghi
        if isinstance(v, str) and len(v) > 120:
            v = v[:120] + "..."
        if v not in (False, None, ''):
            print(f"  {k}: {v}")