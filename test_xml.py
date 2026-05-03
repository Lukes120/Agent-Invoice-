import os, sys, base64
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

print("Scarico 3 XML di fatture NON registrate per analisi...\n")

# Prendo 3 allegati non registrati
attachments = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('registered', '=', False)],
    fields=['id', 'name', 'xml_supplier_id', 'invoices_total',
            'datas', 'linked_invoice_id_xml', 'inconsistencies'],
    limit=3, order='create_date desc'
)

out_dir = Path(__file__).parent / 'xml_samples'
out_dir.mkdir(exist_ok=True)

for att in attachments:
    supplier = att.get('xml_supplier_id')
    supplier_name = supplier[1] if isinstance(supplier, list) else 'N/D'
    print(f"=== {att.get('name')} ===")
    print(f"Fornitore: {supplier_name}")
    print(f"Totale: €{att.get('invoices_total')}")
    print(f"Linked invoice XML: {att.get('linked_invoice_id_xml')}")
    print(f"Inconsistencies: {att.get('inconsistencies')}")

    # Decodifico XML e lo salvo su disco per esame manuale
    if att.get('datas'):
        try:
            xml_bytes = base64.b64decode(att['datas'])
            xml_text = xml_bytes.decode('utf-8', errors='replace')

            # Salvo su file
            safe_name = att['name'].replace('/', '_').replace('\\', '_')
            file_path = out_dir / safe_name
            file_path.write_text(xml_text, encoding='utf-8')
            print(f"Salvato in: {file_path}")

            # Cerco i campi rilevanti
            import re
            print("\n-- Ricerca riferimenti rilevanti --")

            # Numero OdA dal blocco DatiOrdineAcquisto
            oda_match = re.findall(
                r'<DatiOrdineAcquisto>.*?<IdDocumento>(.*?)</IdDocumento>',
                xml_text, re.DOTALL
            )
            if oda_match:
                print(f"  DatiOrdineAcquisto/IdDocumento: {oda_match}")
            else:
                print("  DatiOrdineAcquisto/IdDocumento: NON PRESENTE")

            # Causale
            causale_match = re.findall(r'<Causale>(.*?)</Causale>', xml_text)
            if causale_match:
                print(f"  Causale: {causale_match}")

            # Tutti i 'P' + 5 cifre nel testo
            p_refs = re.findall(r'P\d{5}', xml_text)
            if p_refs:
                unique_refs = list(set(p_refs))
                print(f"  Pattern P##### trovati: {unique_refs}")

            # Conteggio righe fattura
            linee = re.findall(r'<DettaglioLinee>', xml_text)
            print(f"  Numero righe (DettaglioLinee): {len(linee)}")

        except Exception as e:
            print(f"Errore decodifica XML: {e}")

    print()

print(f"File XML salvati in: {out_dir}")
print("Esaminali con Blocco Note o un editor XML per vedere la struttura.")