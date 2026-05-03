import os, sys, base64, re
from pathlib import Path
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

atts = client.get_fatturapa_attachments(
    only_unregistered=True, exclude_self_invoice=True, company_id=1,
)
print(f"Analizzo {len(atts)} allegati cercando CodiceCommessaConvenzione...\n")

trovati = []
for att in atts:
    if not att.get('datas'):
        continue
    try:
        xml_text = base64.b64decode(att['datas']).decode('utf-8', errors='replace')
        matches = re.findall(
            r'<CodiceCommessaConvenzione>(.*?)</CodiceCommessaConvenzione>',
            xml_text
        )
        if matches:
            supplier = att.get('xml_supplier_id')
            sup_name = supplier[1] if isinstance(supplier, list) else 'N/D'
            unique_values = list(set(m.strip() for m in matches))
            trovati.append({
                'supplier': sup_name,
                'name': att['name'][:60],
                'values': unique_values,
            })
    except Exception:
        pass

print(f"Fatture con CodiceCommessaConvenzione compilato: {len(trovati)}\n")

# Statistiche sui pattern
all_values = [v for t in trovati for v in t['values']]
has_p = sum(1 for v in all_values if re.search(r'P\d{4,6}', v, re.IGNORECASE))
has_s = sum(1 for v in all_values if re.search(r'S\d{4,6}', v, re.IGNORECASE))

print(f"Pattern trovati nei valori:")
print(f"  Contengono P##### : {has_p}")
print(f"  Contengono S##### : {has_s}")
print(f"  Totale valori: {len(all_values)}")

print(f"\nPrimi 30 esempi:")
for t in trovati[:30]:
    values_short = ', '.join(t['values'][:3])
    print(f"  [{t['supplier'][:30]:30}] {values_short[:80]}")