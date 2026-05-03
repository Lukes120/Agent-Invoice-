import os, sys, base64
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

print("Analisi dettagliata primi 30 allegati non registrati...\n")

attachments = client.get_fatturapa_attachments(
    only_unregistered=True, limit=30
)

con_oda_xml = []
for att in attachments:
    supplier = att.get('xml_supplier_id')
    sup_name = supplier[1] if isinstance(supplier, list) else 'N/D'

    if not att.get('datas'):
        continue
    data = parse_from_base64(att['datas'])

    if data.oda_riferimenti:
        con_oda_xml.append({
            'supplier': sup_name,
            'totale': att.get('invoices_total'),
            'oda_xml': data.oda_riferimenti,
            'oda_grezzi': data.oda_valori_grezzi,
        })

print(f"Fatture con OdA standard nell'XML: {len(con_oda_xml)} / {len(attachments)}")
print()

if con_oda_xml:
    print("Per ognuna, provo a cercare l'OdA in Odoo:\n")
    for item in con_oda_xml:
        print(f"--- {item['supplier']} (€{item['totale']}) ---")
        print(f"  OdA nell'XML: {item['oda_xml']}")
        print(f"  OdA grezzo: {item['oda_grezzi']}")

        for ref in item['oda_xml']:
            # Cerca esatto
            po = client.search_purchase_order_by_name(ref)
            if po:
                print(f"  OK -> OdA '{ref}' trovato in Odoo (id={po['id']}, "
                      f"stato={po['state']}, totale=€{po['amount_untaxed']})")
            else:
                # Prova anche varianti
                print(f"  NO -> OdA '{ref}' NON trovato")
                # Provo a cercare 'ilike' per vedere se c'è qualcosa di simile
                try:
                    similar = client._call(
                        'purchase.order', 'search_read',
                        [('name', 'ilike', ref)],
                        fields=['id', 'name', 'state'], limit=3
                    )
                    if similar:
                        print(f"    Possibili match simili: "
                              f"{[(p['name'], p['state']) for p in similar]}")
                except Exception as e:
                    pass
        print()

# Controllo anche se esistono OdA con pattern P##### in generale
print("=" * 60)
print("Sanity check: quanti OdA tipo P##### esistono in Odoo?")
print("=" * 60)
try:
    all_po = client._call(
        'purchase.order', 'search_read',
        [('name', 'like', 'P')],
        fields=['id', 'name', 'state'], limit=10, order='id desc'
    )
    print(f"Esempi recenti di OdA:")
    for p in all_po:
        print(f"  {p['name']} (stato={p['state']})")
except Exception as e:
    print(f"Errore: {e}")