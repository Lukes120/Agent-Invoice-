"""Conteggio fatture in 'e-fatture in ingresso' (fatturapa.attachment.in).
Allinea al folder Odoo: action=838, menu_id=263, cids=1 (Ecotel).
"""
import os, sys
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

client = OdooReadOnlyClient(
    os.environ['ODOO_URL'], os.environ['ODOO_DB'],
    os.environ['ODOO_USERNAME'], os.environ['ODOO_PASSWORD'],
)
client.connect()

# 1) Recupero dominio della action 838 per replicarlo esattamente
act = client._call('ir.actions.act_window', 'read', [838],
    fields=['name', 'res_model', 'domain', 'context', 'filter'])
print(f"--- Action 838 ---")
if act:
    a = act[0]
    print(f"  name:    {a.get('name')}")
    print(f"  model:   {a.get('res_model')}")
    print(f"  domain:  {a.get('domain')!r}")
    print(f"  context: {a.get('context')!r}")
print()

# 2) Conteggi: globale + Ecotel-only (cids=1)
COMPANY_ECOTEL = 1
filters = [
    ("Tutto (no filter)", []),
    ("registered=False", [('registered', '=', False)]),
    ("registered=False AND non self_invoice", [('registered', '=', False), ('is_self_invoice', '=', False)]),
    ("Ecotel (company=1)", [('company_id', '=', COMPANY_ECOTEL)]),
    ("Ecotel + registered=False", [('company_id', '=', COMPANY_ECOTEL), ('registered', '=', False)]),
    ("Ecotel + registered=False + non self", [('company_id', '=', COMPANY_ECOTEL), ('registered', '=', False), ('is_self_invoice', '=', False)]),
]

print(f"--- Conteggi ---")
for label, dom in filters:
    n = client._call('fatturapa.attachment.in', 'search_count', dom)
    print(f"  {label:50s}: {n}")
print()

# 3) Tutti i campi del modello (per capire altri filtri possibili)
print(f"--- Campi del modello (top 15 by name) ---")
fields = client._call('fatturapa.attachment.in', 'fields_get', [])
sample = sorted(fields.keys())[:30]
for f in sample:
    info = fields[f]
    print(f"  {f:35s} {info.get('type','?'):12s} {info.get('string','')}")
