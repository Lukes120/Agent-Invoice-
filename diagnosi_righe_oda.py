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

# Prendo P03524
po = client.search_purchase_order_by_name('P03524')

# Leggo TUTTE le righe con tutti i campi disponibili
line_ids = po.get('order_line', [])

# Prima leggo i campi disponibili sul modello purchase.order.line
fields_info = client._call('purchase.order.line', 'fields_get', [])
interesting_fields = [
    f for f in fields_info.keys()
    if 'qty' in f or 'invoic' in f or 'received' in f or 'move' in f or 'picking' in f
]
print(f"Campi potenzialmente interessanti: {interesting_fields}\n")

# Leggo tutte le righe con questi campi
lines = client._call('purchase.order.line', 'search_read',
    [('order_id', '=', po['id'])],
    fields=['id', 'name', 'price_unit', 'product_qty'] + interesting_fields)

# Separo righe "occupate" da "libere"
libere = [l for l in lines if (l.get('price_unit') or 0) == 0]
occupate = [l for l in lines if (l.get('price_unit') or 0) != 0]

print(f"Righe libere: {len(libere)}")
print(f"Righe occupate: {len(occupate)}")
print()

# Mostro differenze tra 1 libera e 1 occupata
if libere and occupate:
    lib = libere[0]
    occ = occupate[0]
    print("="*78)
    print(f"CONFRONTO: riga occupata (id={occ['id']}) vs riga libera (id={lib['id']})")
    print("="*78)
    for k in sorted(set(list(lib.keys()) + list(occ.keys()))):
        v_occ = occ.get(k)
        v_lib = lib.get(k)
        if v_occ != v_lib:
            print(f"  {k}:")
            print(f"    occupata: {v_occ}")
            print(f"    libera:   {v_lib}")

# Mostro completo di 1 riga occupata
print("\n" + "="*78)
print(f"DETTAGLIO COMPLETO riga occupata id={occupate[0]['id']}")
print("="*78)
for k, v in sorted(occupate[0].items()):
    print(f"  {k}: {v}")

# Mostro completo di 1 riga libera
print("\n" + "="*78)
print(f"DETTAGLIO COMPLETO riga libera id={libere[0]['id']}")
print("="*78)
for k, v in sorted(libere[0].items()):
    print(f"  {k}: {v}")