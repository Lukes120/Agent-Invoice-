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

atts = client._call('fatturapa.attachment.in', 'search_read',
    [('xml_supplier_id.name', 'ilike', 'Trenitalia'),
     ('registered', '=', False)],
    fields=['id', 'name', 'xml_supplier_id', 'invoices_total', 'datas'],
    limit=3)

print(f"Trovate {len(atts)} fatture Trenitalia non registrate\n")

for att in atts:
    print(f"{'='*78}")
    print(f"File: {att['name']}")
    
    d = parse_from_base64(att['datas'])
    # Lista campi disponibili per debug
    print(f"Attributi FatturaPAData disponibili:")
    for attr in sorted(vars(d).keys()):
        val = getattr(d, attr)
        if isinstance(val, (str, int, float, bool)) or val is None:
            print(f"    {attr} = {val!r}")
    
    print(f"\nRighe fattura ({len(d.righe)}):")
    for r in d.righe:
        print(f"  L{r.numero_linea}:")
        for attr in sorted(vars(r).keys()):
            val = getattr(r, attr)
            if isinstance(val, (str, int, float, bool)) or val is None:
                print(f"    {attr} = {val!r}")
    
    # Guardo anche l'XML grezzo per trovare campi extra tipici Trenitalia
    xml_text = base64.b64decode(att['datas']).decode('utf-8', errors='replace')
    
    import re
    # AltriDatiGestionali
    matches = re.findall(r'<AltriDatiGestionali>(.*?)</AltriDatiGestionali>',
                         xml_text, re.DOTALL)
    if matches:
        print(f"\n  AltriDatiGestionali ({len(matches)} blocchi):")
        for i, m in enumerate(matches[:5]):
            print(f"    [{i+1}] {m.strip()[:250]}")
    
    # DatiBeniServizi - righe complete
    riga_blocks = re.findall(r'<DettaglioLinee>(.*?)</DettaglioLinee>',
                             xml_text, re.DOTALL)
    if riga_blocks:
        print(f"\n  DettaglioLinee raw ({len(riga_blocks)} righe):")
        for i, m in enumerate(riga_blocks[:3]):
            print(f"    [{i+1}] {m.strip()[:400]}")
    
    print()