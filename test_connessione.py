import os, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

url = os.getenv('ODOO_URL')
db = os.getenv('ODOO_DB')
user = os.getenv('ODOO_USERNAME')
pwd = os.getenv('ODOO_PASSWORD')

print(f"URL: {url}")
print(f"DB:  {db}")
print(f"User: {user}")
print(f"Password: {'*' * len(pwd) if pwd else '(MANCANTE)'}")
print()

print("Tentativo connessione...")
client = OdooReadOnlyClient(url, db, user, pwd)
uid = client.connect()
print(f"OK - autenticato con uid={uid}")
print()

print("Conteggio fatture fornitore degli ultimi 30 giorni...")
from datetime import date, timedelta
date_from = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
date_to = date.today().strftime('%Y-%m-%d')

bills = client.get_vendor_bills(date_from, date_to, ['draft'])
print(f"Fatture in bozza trovate: {len(bills)}")

bills_all = client.get_vendor_bills(date_from, date_to, ['draft', 'posted'])
print(f"Fatture totali (bozza + registrate) ultimi 30 gg: {len(bills_all)}")

if bills:
    print("\nPrime 3 fatture in bozza:")
    for b in bills[:3]:
        partner = b.get('partner_id')
        partner_name = partner[1] if isinstance(partner, list) else 'N/D'
        print(f"  {b.get('name')} | {partner_name} | {b.get('invoice_date')} | €{b.get('amount_total')}")

print("\nTest completato.")