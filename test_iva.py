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

# Riprocesso le 3 con OdA trovato
for po_name, att_id_supplier in [
    ('P03732', 'Electro Rent'),
    ('P04532', 'ARROW ECS'),
    ('P04663', 'Smart IT'),
]:
    # Ripesco l'allegato del fornitore
    atts = client._call('fatturapa.attachment.in', 'search_read',
        [('xml_supplier_id.name', 'ilike', att_id_supplier),
         ('registered', '=', False)],
        fields=['id', 'name', 'invoices_total', 'datas'],
        limit=1, order='create_date desc')
    if not atts:
        print(f"Nessun allegato per {att_id_supplier}")
        continue
    att = atts[0]
    d = parse_from_base64(att['datas'])

    po = client.search_purchase_order_by_name(po_name)

    print(f"\n=== {att_id_supplier} - OdA {po_name} ===")
    print(f"FATTURA (dall'XML):")
    print(f"  Imponibile: €{d.imponibile_totale:.2f}")
    print(f"  Imposta:    €{d.imposta_totale:.2f}")
    print(f"  Totale doc: €{d.importo_totale:.2f}")
    print(f"  Righe:")
    for line in d.righe:
        print(f"    #{line.numero_linea} qty={line.quantita} "
              f"x €{line.prezzo_unitario} = €{line.prezzo_totale} "
              f"(IVA {line.aliquota_iva}%)")
    print(f"\nORDINE DI ACQUISTO (da Odoo):")
    print(f"  amount_untaxed: €{po['amount_untaxed']:.2f}")
    print(f"  amount_tax:     €{po['amount_tax']:.2f}")
    print(f"  amount_total:   €{po['amount_total']:.2f}")

    # Leggo le righe OdA per confronto
    pol = client.get_purchase_order_lines(po.get('order_line', []))
    print(f"  Righe OdA:")
    for p in pol:
        prod = p.get('product_id')
        prod_name = prod[1] if isinstance(prod, list) else '?'
        print(f"    qty={p['product_qty']} x €{p['price_unit']} = "
              f"€{p['price_subtotal']} | {prod_name[:50]}")

    print(f"\nCONFRONTO:")
    diff_imponibile = d.imponibile_totale - float(po['amount_untaxed'])
    diff_totale = d.importo_totale - float(po['amount_total'])
    print(f"  Diff imponibile (XML - OdA): €{diff_imponibile:+.2f}")
    print(f"  Diff totale IVA inclusa:      €{diff_totale:+.2f}")