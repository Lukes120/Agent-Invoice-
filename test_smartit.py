import os, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64
from core.keyword_rules import classify_line_by_keyword

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

# Trovo l'allegato Smart IT
atts = client._call('fatturapa.attachment.in', 'search_read',
    [('xml_supplier_id.name', 'ilike', 'Smart IT'),
     ('registered', '=', False)],
    fields=['id', 'name', 'invoices_total', 'datas'],
    limit=1, order='create_date desc')

if not atts:
    print("Nessun allegato Smart IT trovato")
else:
    att = atts[0]
    d = parse_from_base64(att['datas'])
    print(f"Fattura Smart IT: {d.numero}")
    print(f"Imponibile XML: €{d.imponibile_totale}")
    print()
    print(f"Righe ({len(d.righe)}):")
    for line in d.righe:
        print(f"  #{line.numero_linea} qty={line.quantita} x €{line.prezzo_unitario} = €{line.prezzo_totale}")
        print(f"    descrizione: '{line.descrizione}'")

        # Verifico se la mia keyword classifier la cattura
        result = classify_line_by_keyword(line.descrizione)
        if result:
            conto_key, conto_codice, categoria = result
            print(f"    >>> KEYWORD MATCH! -> categoria={categoria}, conto={conto_codice}")
            print(f"    Questa riga viene sottratta dal confronto imponibile!")
        else:
            print(f"    (nessun keyword match -> trattata come merce)")