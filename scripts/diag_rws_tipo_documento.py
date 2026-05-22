"""
Verifica stato bug RWS sulla colonna tipo_documento di
fatturapa.attachment.in (sparita dal deploy 15/05).

Step:
  1. fields_get → il campo esiste a livello modello?
  2. read di tipo_documento su un attachment recente → la colonna in PG c'e'?
  3. search filtrando per tipo_documento → l'indice funziona?
  4. ir.module.module → versione modulo l10n_it_fiscal_document_type_custom
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
import xmlrpc.client


client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'],
    db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'],
    password=os.environ['ODOO_PASSWORD'],
)
client.connect()

MODEL = 'fatturapa.attachment.in'
FIELD = 'tipo_documento'

print("=" * 90)
print(f"DIAGNOSI BUG RWS - campo '{FIELD}' su modello '{MODEL}'")
print("=" * 90)

# 1) fields_get -> campo esiste?
print("\n[1/4] fields_get...")
try:
    fields_def = client._call(MODEL, 'fields_get', [],
                              attributes=['string', 'type', 'store', 'required'])
    if FIELD in fields_def:
        f = fields_def[FIELD]
        print(f"  OK: campo '{FIELD}' DEFINITO sul modello")
        print(f"     - string  : {f.get('string')}")
        print(f"     - type    : {f.get('type')}")
        print(f"     - store   : {f.get('store')}")
        print(f"     - required: {f.get('required')}")
    else:
        print(f"  WARN: campo '{FIELD}' NON definito sul modello")
        # vediamo cosa c'e' di simile
        like = [k for k in fields_def if 'tipo' in k.lower() or 'document_type' in k.lower()]
        if like:
            print(f"     Campi simili: {like}")
except Exception as e:
    print(f"  ERRORE fields_get: {e}")

# 2) read del campo su un record recente -> la colonna in PG c'e'?
print("\n[2/4] read tipo_documento su un attachment recente...")
try:
    atts = client._call(
        MODEL, 'search_read',
        [('company_id', '=', 1)],
        fields=['id', 'att_name', 'tipo_documento'],
        order='create_date desc',
        limit=3,
    )
    print(f"  OK: read riuscita su {len(atts)} attachment")
    for a in atts:
        print(f"     id={a['id']} | tipo_documento={a.get('tipo_documento')!r:<10} "
              f"| {a.get('att_name', '')[:60]}")
except xmlrpc.client.Fault as e:
    msg = str(e.faultString)
    if 'tipo_documento' in msg.lower() and ('does not exist' in msg.lower()
                                            or 'invalid field' in msg.lower()):
        print(f"  ERRORE: il bug e' ANCORA PRESENTE")
        print(f"     {msg.splitlines()[-1][:200]}")
    else:
        print(f"  ERRORE diverso: {msg[:300]}")
except Exception as e:
    print(f"  ERRORE imprevisto: {e}")

# 3) search filtrando per tipo_documento -> l'indice/colonna PG funziona?
print("\n[3/4] search filtro tipo_documento='TD01'...")
try:
    cnt = client._call(MODEL, 'search_count',
                       [('tipo_documento', '=', 'TD01'),
                        ('company_id', '=', 1)])
    print(f"  OK: trovati {cnt} attachment Ecotel con tipo_documento=TD01")
except xmlrpc.client.Fault as e:
    msg = str(e.faultString)
    if 'tipo_documento' in msg.lower():
        print(f"  ERRORE: filtro su tipo_documento ancora rotto")
        print(f"     {msg.splitlines()[-1][:200]}")
    else:
        print(f"  ERRORE diverso: {msg[:300]}")
except Exception as e:
    print(f"  ERRORE imprevisto: {e}")

# 4) Modulo l10n_it_fiscal_document_type_custom
print("\n[4/4] ir.module.module - versione modulo custom RWS...")
try:
    mods = client._call('ir.module.module', 'search_read',
                       [('name', 'ilike', 'fiscal_document_type')],
                       fields=['id', 'name', 'state', 'installed_version',
                               'latest_version', 'summary'],
                       limit=20)
    if not mods:
        print("  WARN: nessun modulo con nome ilike 'fiscal_document_type'")
    for m in mods:
        print(f"  {m['name']!r}")
        print(f"     state             : {m.get('state')}")
        print(f"     installed_version : {m.get('installed_version')}")
        print(f"     latest_version    : {m.get('latest_version')}")
        if m.get('summary'):
            print(f"     summary           : {m.get('summary')[:100]}")
except Exception as e:
    print(f"  ERRORE: {e}")

# Anche moduli RWS in generale
print("\n[bonus] ir.module.module - moduli con 'rws' nel nome/author...")
try:
    mods = client._call('ir.module.module', 'search_read',
                       [('name', 'ilike', 'rws')],
                       fields=['id', 'name', 'state', 'installed_version',
                               'latest_version'],
                       limit=20)
    if not mods:
        print("  Nessun modulo con nome ilike 'rws'")
    for m in mods:
        print(f"  {m['name']!r} | state={m.get('state')} | ver={m.get('installed_version')}")
except Exception as e:
    print(f"  ERRORE: {e}")

print("\nDONE.")
