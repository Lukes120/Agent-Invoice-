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

# Cerco l'allegato specifico
atts = client._call('fatturapa.attachment.in', 'search_read',
    [('name', 'ilike', '204876aa-IT0000001021160328')],
    fields=['id', 'name', 'xml_supplier_id', 'invoices_total', 'datas',
            'registered'],
    limit=1)

if not atts:
    print("Allegato non trovato")
    sys.exit(1)

att = atts[0]
supplier = att.get('xml_supplier_id')
sup_name = supplier[1] if isinstance(supplier, list) else '?'
sup_id = supplier[0] if isinstance(supplier, list) else None

print(f"Allegato: {att['name']}")
print(f"Fornitore: {sup_name} (id={sup_id})")
print(f"Totale fattura (invoices_total): €{att.get('invoices_total')}")

# Parso XML per imponibile
d = parse_from_base64(att['datas'])
print(f"Imponibile XML: €{d.imponibile_totale}")
print(f"Totale XML: €{d.importo_totale}")
print(f"OdA rilevati XML: {d.oda_riferimenti}")

# Cerco in Odoo tutti gli OdA di questo fornitore
print(f"\n--- OdA in Odoo per {sup_name} ---")
all_pos = client._call(
    'purchase.order', 'search_read',
    [('partner_id', '=', sup_id),
     ('state', 'in', ['purchase', 'done'])],
    fields=['id', 'name', 'state', 'invoice_status',
            'amount_untaxed', 'amount_total', 'date_order'],
    limit=50, order='date_order desc'
)
print(f"Trovati {len(all_pos)} OdA:")
for po in all_pos:
    match_marker = ""
    if abs(float(po['amount_untaxed']) - d.imponibile_totale) < 1.0:
        match_marker = " *** MATCH IMPONIBILE ***"
    elif abs(float(po['amount_total']) - d.importo_totale) < 1.0:
        match_marker = " *** MATCH TOTALE IVA INC ***"
    print(f"  {po['name']} | imponibile €{po['amount_untaxed']:.2f} | "
          f"totale €{po['amount_total']:.2f} | "
          f"invoice_status={po['invoice_status']} | "
          f"data {po['date_order'][:10]}{match_marker}")

# Stesso test su tutti gli OdA (non solo questo fornitore) con questo imponibile esatto
print(f"\n--- Tutti gli OdA con imponibile €{d.imponibile_totale:.2f} ---")
all_same_amount = client._call(
    'purchase.order', 'search_read',
    [('amount_untaxed', '>=', d.imponibile_totale - 0.5),
     ('amount_untaxed', '<=', d.imponibile_totale + 0.5),
     ('state', 'in', ['purchase', 'done'])],
    fields=['id', 'name', 'partner_id', 'amount_untaxed',
            'invoice_status', 'date_order'],
    limit=20
)
print(f"Trovati {len(all_same_amount)} OdA con questo importo:")
for po in all_same_amount:
    p = po['partner_id']
    pname = p[1] if isinstance(p, list) else '?'
    print(f"  {po['name']} | {pname} | €{po['amount_untaxed']:.2f} | "
          f"status={po['invoice_status']} | data {po['date_order'][:10]}")