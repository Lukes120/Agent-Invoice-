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

for oda_name in ['P03396', 'P04474', 'P03263']:
    print(f"\n{'='*60}")
    print(f"=== OdA {oda_name} ===")
    print('='*60)

    po = client.search_purchase_order_by_name(oda_name)
    if not po:
        print("  NON TROVATO")
        continue

    print(f"Fornitore: {po['partner_id']}")
    print(f"Stato: {po['state']}")
    print(f"invoice_status: {po['invoice_status']}")
    print(f"amount_untaxed: €{po['amount_untaxed']:.2f}")
    print(f"amount_total: €{po['amount_total']:.2f}")

    # Account.move collegati (fatture Odoo registrate)
    moves = client._call('account.move', 'search_read',
        [('invoice_origin', '=', oda_name),
         ('move_type', '=', 'in_invoice')],
        fields=['id', 'name', 'state', 'amount_untaxed', 'amount_total',
                'invoice_date', 'ref', 'partner_id'],
        order='invoice_date desc', limit=30)
    print(f"\nAccount.move collegati (invoice_origin={oda_name}): {len(moves)}")
    for m in moves:
        print(f"  [{m['state']}] {m.get('name', '?')} | ref={m.get('ref', '?')} | "
              f"€{m['amount_untaxed']:.2f} | {m.get('invoice_date', '?')}")

    # Attachment fatturapa con stesso riferimento OdA
    # (per capire quante sono pendenti)
    atts = client._call('fatturapa.attachment.in', 'search_read',
        [('registered', '=', False)],
        fields=['id', 'name', 'invoices_total', 'invoices_date',
                'xml_supplier_id', 'in_invoice_ids'],
        limit=500)

    # Filtro manuale per OdA testuale
    import base64, re
    filtered = []
    for att in atts:
        try:
            if not att.get('in_invoice_ids'):
                # Potrebbe non avere datas, skip
                continue
        except:
            continue

    # Giro sulla fattura specifica che conosciamo
    print(f"\n(Attachment non registrati: {len(atts)} totali. Filter XML "
          "troppo costoso in batch - skip)")