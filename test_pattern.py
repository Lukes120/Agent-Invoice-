import os, sys, base64, re
from pathlib import Path
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

print("Scarico campione di 50 XML da analizzare...\n")

attachments = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('registered', '=', False)],
    fields=['id', 'name', 'xml_supplier_id', 'invoices_total', 'datas'],
    limit=50, order='create_date desc'
)

stats = {
    'totale': 0,
    'con_dati_ordine_acquisto': 0,
    'senza_dati_ordine_acquisto': 0,
    'id_documento_valori': [],
    'causale_valori': [],
    'fornitori_senza_oda': Counter(),
    'fornitori_con_oda': Counter(),
    'pattern_p5': 0,      # P + 5 cifre
    'pattern_p_generic': 0,  # P + cifre varie
    'pattern_s': 0,       # S + cifre
    'pattern_po': 0,      # PO / PO-
    'pattern_ord': 0,     # ORD
}

for att in attachments:
    stats['totale'] += 1
    supplier = att.get('xml_supplier_id')
    supplier_name = supplier[1] if isinstance(supplier, list) else 'N/D'

    if not att.get('datas'):
        continue
    try:
        xml_text = base64.b64decode(att['datas']).decode('utf-8', errors='replace')
    except Exception:
        continue

    has_oda = False

    # Cerco blocco DatiOrdineAcquisto
    oda_blocks = re.findall(
        r'<DatiOrdineAcquisto>(.*?)</DatiOrdineAcquisto>',
        xml_text, re.DOTALL
    )
    if oda_blocks:
        has_oda = True
        stats['con_dati_ordine_acquisto'] += 1
        stats['fornitori_con_oda'][supplier_name] += 1
        for block in oda_blocks:
            ids = re.findall(r'<IdDocumento>(.*?)</IdDocumento>', block)
            for id_doc in ids:
                stats['id_documento_valori'].append((supplier_name, id_doc.strip()))
    else:
        stats['senza_dati_ordine_acquisto'] += 1
        stats['fornitori_senza_oda'][supplier_name] += 1

    # Causale
    causali = re.findall(r'<Causale>(.*?)</Causale>', xml_text)
    for c in causali:
        stats['causale_valori'].append((supplier_name, c.strip()[:100]))

    # Conta i vari pattern nel testo completo dell'XML
    if re.search(r'\bP\d{5}\b', xml_text):
        stats['pattern_p5'] += 1
    if re.search(r'\bP\d{3,7}\b', xml_text):
        stats['pattern_p_generic'] += 1
    if re.search(r'\bS\d{4,6}\b', xml_text):
        stats['pattern_s'] += 1
    if re.search(r'\bPO[-/]?\d+', xml_text):
        stats['pattern_po'] += 1
    if re.search(r'\bORD[-/]?\d+', xml_text):
        stats['pattern_ord'] += 1

print("=" * 60)
print("STATISTICHE GLOBALI")
print("=" * 60)
print(f"Fatture analizzate: {stats['totale']}")
print(f"Con <DatiOrdineAcquisto>: {stats['con_dati_ordine_acquisto']}")
print(f"Senza <DatiOrdineAcquisto>: {stats['senza_dati_ordine_acquisto']}")
print()
print("Fatture con pattern testuali nel contenuto XML:")
print(f"  P##### (5 cifre):     {stats['pattern_p5']}")
print(f"  P##-####### (varie):  {stats['pattern_p_generic']}")
print(f"  S#####:               {stats['pattern_s']}")
print(f"  PO/PO-###:            {stats['pattern_po']}")
print(f"  ORD/ORD-###:          {stats['pattern_ord']}")

print()
print("=" * 60)
print("VALORI <IdDocumento> IN <DatiOrdineAcquisto>")
print("=" * 60)
for supplier, id_doc in stats['id_documento_valori'][:40]:
    print(f"  [{supplier[:30]:30}] -> '{id_doc}'")

print()
print("=" * 60)
print("TOP 10 FORNITORI SENZA OdA nel XML")
print("=" * 60)
for supplier, count in stats['fornitori_senza_oda'].most_common(10):
    print(f"  {supplier}: {count} fatture")

print()
print("=" * 60)
print("CAUSALI (primi 20, primi 100 caratteri)")
print("=" * 60)
for supplier, causale in stats['causale_valori'][:20]:
    print(f"  [{supplier[:25]:25}] {causale}")