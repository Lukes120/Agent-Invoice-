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

# ============================================================
# 1. CAMPI DISPONIBILI SU fatturapa.attachment.in
# ============================================================
print("="*78)
print("1. CAMPI RILEVANTI DI fatturapa.attachment.in")
print("="*78)

fields_info = client._call('fatturapa.attachment.in', 'fields_get', [])
interesting = [
    f for f in fields_info.keys()
    if any(k in f.lower() for k in
           ['regist', 'state', 'in_invoice', 'move', 'attach', 'status',
            'processed', 'done', 'linked'])
]
print(f"\nCampi potenzialmente interessanti ({len(interesting)}):")
for f in sorted(interesting):
    info = fields_info[f]
    print(f"  {f}: type={info.get('type')}, label={info.get('string','?')}")
    if info.get('type') == 'selection':
        print(f"     valori: {info.get('selection')}")

# ============================================================
# 2. CONFRONTO FRA UN ATTACHMENT GIA' REGISTRATO E UNO NON REGISTRATO
# ============================================================
print("\n" + "="*78)
print("2. CONFRONTO ATTACHMENT REGISTRATO vs NON REGISTRATO")
print("="*78)

# Trova 1 attachment già registrato (dovrebbe avere registered=True)
atts_reg = client._call('fatturapa.attachment.in', 'search_read',
    [('registered', '=', True), ('xml_supplier_id.name', 'ilike', 'Trenitalia')],
    fields=['id', 'name'] + interesting,
    limit=1)

# Trova 1 attachment non registrato
atts_nonreg = client._call('fatturapa.attachment.in', 'search_read',
    [('registered', '=', False), ('xml_supplier_id.name', 'ilike', 'Trenitalia')],
    fields=['id', 'name'] + interesting,
    limit=1)

if atts_reg and atts_nonreg:
    r = atts_reg[0]
    n = atts_nonreg[0]
    print(f"\nATTACHMENT REGISTRATO: id={r['id']} | {r['name']}")
    print(f"ATTACHMENT NON REGISTRATO: id={n['id']} | {n['name']}")
    print("\nDifferenze:")
    for k in sorted(set(list(r.keys()) + list(n.keys()))):
        vr = r.get(k)
        vn = n.get(k)
        if vr != vn:
            print(f"  {k}:")
            print(f"    registrato: {vr}")
            print(f"    non-reg:    {vn}")

# ============================================================
# 3. CAMPI RILEVANTI DI account.move PER COMPETENZA IVA
# ============================================================
print("\n" + "="*78)
print("3. CAMPI RILEVANTI DI account.move PER DATA COMPETENZA IVA")
print("="*78)

move_fields = client._call('account.move', 'fields_get', [])
date_fields = [
    f for f in move_fields.keys()
    if any(k in f.lower() for k in ['date', 'period', 'competenz', 'tax_date', 'esigib'])
]
print(f"\nCampi data/competenza ({len(date_fields)}):")
for f in sorted(date_fields):
    info = move_fields[f]
    print(f"  {f}: type={info.get('type')}, label={info.get('string','?')}")

# ============================================================
# 4. ISPEZIONO LA BOZZA CREATA DALL'AGENT (se ancora c'è)
# ============================================================
print("\n" + "="*78)
print("4. ULTIMA BOZZA TRENITALIA (se presente in Odoo)")
print("="*78)

moves = client._call('account.move', 'search_read',
    [('partner_id.name', 'ilike', 'Trenitalia'),
     ('move_type', '=', 'in_invoice'),
     ('state', '=', 'draft')],
    fields=['id', 'name', 'ref', 'invoice_date', 'date',
            'invoice_date_due', 'amount_untaxed', 'amount_total'],
    order='create_date desc', limit=3)

for m in moves:
    print(f"\nMove id={m['id']}")
    for k, v in sorted(m.items()):
        print(f"  {k}: {v}")

# ============================================================
# 5. COLLEGAMENTO attachment <-> account.move
# ============================================================
print("\n" + "="*78)
print("5. COME account.move COLLEGA fatturapa.attachment.in")
print("="*78)

# Cerco campi su account.move che puntano a fatturapa
linked_fields = [
    f for f in move_fields.keys()
    if 'fatturapa' in f.lower() or 'e_invoice' in f.lower() or 'sdi' in f.lower()
]
print(f"\nCampi collegamento fatturapa ({len(linked_fields)}):")
for f in sorted(linked_fields):
    info = move_fields[f]
    print(f"  {f}: type={info.get('type')}, relation={info.get('relation','-')}")

# Se esiste il campo, leggilo su un move già registrato collegato a P03524
if 'fatturapa_attachment_in_id' in move_fields:
    reg_moves = client._call('account.move', 'search_read',
        [('invoice_origin', '=', 'P03524'),
         ('state', '=', 'posted'),
         ('fatturapa_attachment_in_id', '!=', False)],
        fields=['id', 'name', 'ref', 'fatturapa_attachment_in_id'],
        limit=3)
    print(f"\nEsempi di account.move Trenitalia POSTED con fatturapa collegato:")
    for m in reg_moves:
        print(f"  id={m['id']} | {m['name']} | ref={m['ref']} | att={m.get('fatturapa_attachment_in_id')}")