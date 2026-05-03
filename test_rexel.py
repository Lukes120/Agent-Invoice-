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

# 1. Verifico OdA P04235
print("=== OdA P04235 ===")
po = client.search_purchase_order_by_name('P04235')
if po:
    print(f"id={po['id']}")
    print(f"fornitore: {po['partner_id']}")
    print(f"stato: {po['state']}")
    print(f"invoice_status: {po['invoice_status']}")
    print(f"amount_untaxed: €{po['amount_untaxed']}")
    print(f"amount_total: €{po['amount_total']}")
    print(f"data: {po.get('date_order', '')[:10]}")

    lines = client.get_purchase_order_lines(po.get('order_line', []))
    print(f"\nRighe OdA ({len(lines)}):")
    for l in lines:
        prod = l.get('product_id')
        prod_name = prod[1] if isinstance(prod, list) else '?'
        print(f"  qty={l['product_qty']} x €{l['price_unit']} = €{l['price_subtotal']} | {prod_name[:60]}")
else:
    print("OdA P04235 NON trovato")

# 2. Recupero fattura REXEL 26FE015265
print("\n=== Fattura REXEL 26FE015265 ===")
atts = client._call('fatturapa.attachment.in', 'search_read',
    [('xml_supplier_id.name', 'ilike', 'REXEL')],
    fields=['id', 'name', 'xml_supplier_id', 'invoices_total', 'datas',
            'registered'], limit=20, order='create_date desc')

found = None
for att in atts:
    d = parse_from_base64(att['datas'])
    if '26FE015265' in d.numero or '015265' in d.numero:
        found = (att, d)
        break

if found:
    att, d = found
    print(f"File: {att['name']}")
    print(f"Numero: {d.numero}")
    print(f"Imponibile XML: €{d.imponibile_totale}")
    print(f"Totale XML: €{d.importo_totale}")
    print(f"\nOdA nel campo DatiOrdineAcquisto: {d.oda_riferimenti}")
    print(f"OdA grezzi: {d.oda_valori_grezzi}")
    print(f"Commesse rilevate: {d.commessa_riferimenti}")

    # Cerco riferimenti nel body raw dell'XML
    import base64, re
    xml_text = base64.b64decode(att['datas']).decode('utf-8', errors='replace')

    print(f"\n--- Ricerca pattern nel testo XML ---")
    # Pattern P+cifre
    matches_p = re.findall(r'P\d{4,6}', xml_text)
    print(f"Pattern P##### trovati nel testo: {set(matches_p)}")

    # Cerco 04235 come stringa
    if '04235' in xml_text:
        print(f"Stringa '04235' trovata nel testo")
        # Mostro il contesto
        idx = xml_text.find('04235')
        print(f"Contesto: ...{xml_text[max(0,idx-100):idx+100]}...")
    else:
        print(f"Stringa '04235' NON trovata")

    # Cerco numeri d'ordine nelle righe (RiferimentoNumeroLinea, etc.)
    ord_blocks = re.findall(r'<DatiOrdineAcquisto>(.*?)</DatiOrdineAcquisto>', xml_text, re.DOTALL)
    print(f"Blocchi DatiOrdineAcquisto: {len(ord_blocks)}")
    for i, blk in enumerate(ord_blocks[:3]):
        print(f"  Blocco {i+1}: {blk[:200]}")

    # AltriDatiGestionali
    altri = re.findall(r'<AltriDatiGestionali>(.*?)</AltriDatiGestionali>', xml_text, re.DOTALL)
    print(f"Blocchi AltriDatiGestionali: {len(altri)}")
    for i, blk in enumerate(altri[:3]):
        print(f"  Blocco {i+1}: {blk[:200]}")

    # Vedo prime 2000 caratteri del XML
    print(f"\n--- Primi 2000 caratteri dell'XML ---")
    print(xml_text[:2000])
else:
    print("Fattura non trovata")