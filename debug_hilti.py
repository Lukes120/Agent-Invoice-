import os, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64, _extract_oda_from_text

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

# Cerca tutti gli allegati HILTI non registrati
atts = client._call('fatturapa.attachment.in', 'search_read',
    [('xml_supplier_id.name', 'ilike', 'HILTI'),
     ('registered', '=', False)],
    fields=['id', 'name', 'xml_supplier_id', 'invoices_total', 'datas',
            'invoices_date'],
    order='invoices_date desc', limit=20)

print(f"Trovati {len(atts)} allegati HILTI non registrati\n")

for att in atts:
    d = parse_from_base64(att['datas'])
    
    # Mostro solo se numero fattura corrisponde alle 3 di interesse
    if d.numero not in ['1765597487', '1765628120', '1765628118']:
        continue
    
    print(f"{'='*70}")
    print(f"=== Fattura HILTI {d.numero} - {att.get('invoices_date')} ===")
    print('='*70)
    print(f"File: {att['name']}")
    print(f"Imponibile: €{d.imponibile_totale}")
    print()
    print(f"OdA strutturati (DatiOrdineAcquisto/IdDocumento): {d.oda_riferimenti}")
    print(f"OdA valori grezzi: {d.oda_valori_grezzi}")
    print(f"OdA testuali (da descrizioni/causali): {d.oda_riferimenti_testuali}")
    print(f"Causali: {d.causali}")
    print()

    # Dettaglio righe
    print(f"Righe ({len(d.righe)}):")
    for r in d.righe:
        print(f"  L{r.numero_linea}: {r.descrizione[:100]}")
        if r.descrizione:
            extracted = _extract_oda_from_text(r.descrizione)
            if extracted:
                print(f"    -> OdA estratti: {extracted}")

    # Blocchi DatiOrdineAcquisto
    import base64, re
    xml_text = base64.b64decode(att['datas']).decode('utf-8', errors='replace')
    ord_blocks = re.findall(r'<DatiOrdineAcquisto>(.*?)</DatiOrdineAcquisto>',
                            xml_text, re.DOTALL)
    print(f"\nBlocchi <DatiOrdineAcquisto>: {len(ord_blocks)}")
    for i, blk in enumerate(ord_blocks[:5]):
        print(f"  Blocco {i+1}: {blk.strip()[:250]}")
    print()