import os, sys
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

# Cerco TUTTE le fatture FRANCIONI non registrate
atts = client._call('fatturapa.attachment.in', 'search_read',
    [('xml_supplier_id.name', 'ilike', 'FRANCIONI'),
     ('registered', '=', False)],
    fields=['id', 'name', 'xml_supplier_id', 'invoices_total',
            'invoices_date', 'datas'],
    order='invoices_date desc', limit=10)

print(f"Trovate {len(atts)} fatture FRANCIONI non registrate\n")

for att in atts:
    print(f"{'='*70}")
    print(f"File: {att['name']}")
    print(f"Fornitore: {att.get('xml_supplier_id')}")
    print(f"Totale Odoo: €{att.get('invoices_total')}")

    d = parse_from_base64(att['datas'])
    print(f"Numero fattura: {d.numero}")
    print(f"Imponibile XML: €{d.imponibile_totale:.2f}")
    print(f"Totale XML: €{d.importo_totale:.2f}")
    print(f"OdA strutturati: {d.oda_riferimenti}")
    print(f"OdA testuali: {d.oda_riferimenti_testuali}")
    print()

# Adesso cerco OdA P03850
print(f"{'='*70}")
print("=== OdA P03850 (dove dovrebbe matchare FRANCIONI) ===")
po = client.search_purchase_order_by_name('P03850')
if po:
    partner_id = po['partner_id'][0] if isinstance(po['partner_id'], list) else None
    print(f"Fornitore OdA: {po['partner_id']} (id={partner_id})")

    # Confronto con i partner_id delle fatture
    for att in atts:
        att_partner = att.get('xml_supplier_id')
        att_partner_id = att_partner[0] if isinstance(att_partner, list) else None
        match = "MATCH!" if att_partner_id == partner_id else "DIVERSI!"
        print(f"  Fattura {att['name'][:50]} partner_id={att_partner_id} -> {match}")