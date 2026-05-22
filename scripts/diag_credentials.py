"""Diagnostica credentials.env senza esporre la chiave intera."""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

env_path = ROOT / 'config' / 'credentials.env'
print(f"env file: {env_path}")
print(f"exists: {env_path.exists()}, size: {env_path.stat().st_size} bytes")

# Controllo BOM e encoding raw
with open(env_path, 'rb') as f:
    raw = f.read()
print(f"first 3 bytes (hex): {raw[:3].hex()}  (BOM utf-8 sarebbe efbbbf)")
print(f"contains carriage returns: {b'\\r' in raw}")
print(f"total lines: {raw.count(b'\\n') + 1}")

load_dotenv(env_path)

for var in ['ODOO_URL', 'ODOO_DB', 'ODOO_USERNAME', 'ODOO_PASSWORD']:
    val = os.environ.get(var, '<MANCANTE>')
    if var == 'ODOO_PASSWORD' and val != '<MANCANTE>':
        # mascherato: lunghezza + primi 4 + ultimi 4
        masked = f"{val[:4]}...{val[-4:]} (len={len(val)})"
        # warning se contiene caratteri sospetti
        suspicious = []
        if val != val.strip():
            suspicious.append("LEADING/TRAILING WHITESPACE!")
        if '"' in val or "'" in val:
            suspicious.append("QUOTES IN VALUE!")
        if any(c.isspace() for c in val):
            suspicious.append("INTERNAL WHITESPACE!")
        if '\r' in val or '\n' in val:
            suspicious.append("NEWLINES IN VALUE!")
        warn = " [" + " ".join(suspicious) + "]" if suspicious else ""
        print(f"  {var}: {masked}{warn}")
    else:
        # gli altri valori sono visibili
        suspicious = []
        if val != val.strip():
            suspicious.append("WHITESPACE!")
        warn = " [" + " ".join(suspicious) + "]" if suspicious else ""
        print(f"  {var}: {val!r}{warn}")

# Test di autenticazione vero
print()
print("--- test authenticate XML-RPC ---")
import xmlrpc.client
url = os.environ['ODOO_URL']
db = os.environ['ODOO_DB']
user = os.environ['ODOO_USERNAME']
pwd = os.environ['ODOO_PASSWORD']
try:
    common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
    # version() funziona senza autenticazione
    v = common.version()
    print(f"  server version: {v}")
    uid = common.authenticate(db, user, pwd, {})
    print(f"  authenticate result: uid={uid}")
    if not uid:
        print("  -> authenticate returned False/0 = chiave o utente non validi per questo DB")
except Exception as e:
    print(f"  Exception: {type(e).__name__}: {str(e)[:300]}")
