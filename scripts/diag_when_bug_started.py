"""
Diagnostica per capire DA QUANDO si manifesta il bug 'tipo_documento does not exist'.
Read-only, no scritture.

Controlla:
1. fields_get su fatturapa.attachment.in → tipo_documento è dichiarato come field Python?
2. Quanti account.move hanno fatturapa_attachment_in_id valorizzato (= storico dei link funzionanti)?
3. Moduli installati legati a fatturapa / l10n_it: installed_version, latest_version, state, write_date.
"""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

# Output anche su file
OUTPUT_FILE = ROOT / 'output' / 'xml.txt'
OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
_logf = open(OUTPUT_FILE, 'w', encoding='utf-8')
_stdout = sys.stdout

class _Tee:
    def __init__(self, *s): self.s = s
    def write(self, d):
        for x in self.s:
            try: x.write(d)
            except: pass
    def flush(self):
        for x in self.s:
            try: x.flush()
            except: pass

sys.stdout = _Tee(_stdout, _logf)

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'], db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'], password=os.environ['ODOO_PASSWORD'])
client.connect()

print("=" * 80)
print("DIAGNOSTICA: da quando si manifesta il bug tipo_documento?")
print("=" * 80)

# === STEP 1: fields_get su fatturapa.attachment.in ===
print("\n--- STEP 1: campi dichiarati su fatturapa.attachment.in ---")
try:
    fields = client._call('fatturapa.attachment.in', 'fields_get', [], attributes=['type', 'store', 'string', 'compute'])
    # cerco campi interessanti
    interesting = ['tipo_documento', 'registered', 'fatturapa_attachment_in_id',
                   'invoices_number', 'invoices_total', 'xml_supplier_id',
                   'in_invoice_ids', 'datas']
    print(f"  Totale campi: {len(fields)}")
    for fname in interesting:
        if fname in fields:
            f = fields[fname]
            print(f"  ✓ {fname}: type={f.get('type')} store={f.get('store')} "
                  f"compute={f.get('compute')} label={f.get('string')!r}")
        else:
            print(f"  ✗ {fname}: NON dichiarato")
    # cerco se c'è 'tipo_documento' o varianti
    related = [k for k in fields if 'tipo' in k.lower() or 'documento' in k.lower()]
    print(f"  Campi con 'tipo' o 'documento' nel nome: {related}")
except Exception as e:
    print(f"  fields_get FAIL: {str(e)[:200]}")

# === STEP 2: storico account.move con fatturapa_attachment_in_id valorizzato ===
print("\n--- STEP 2: quanti account.move hanno fatturapa_attachment_in_id valorizzato? ---")
try:
    n_linked = client._call('account.move', 'search_count',
                            [('fatturapa_attachment_in_id', '!=', False),
                             ('company_id', '=', 1)])
    print(f"  account.move Ecotel con link XML valorizzato: {n_linked}")
except Exception as e:
    print(f"  search_count FAIL: {str(e)[:200]}")

# Cerco il PIÙ RECENTE move con link valorizzato (per capire da quando si è rotto)
# uso fields=['id','create_date','invoice_date','ref'] SENZA fatturapa_attachment_in_id
# per non innescare cascade.
print("\n--- STEP 2b: ultimi 10 account.move con link XML valorizzato (senza expandere il M2o) ---")
try:
    recent = client._call('account.move', 'search_read',
        [('fatturapa_attachment_in_id', '!=', False),
         ('company_id', '=', 1)],
        fields=['id', 'create_date', 'invoice_date', 'ref', 'partner_id', 'amount_total'],
        order='create_date desc',
        limit=10)
    for m in recent:
        p = m.get('partner_id')
        print(f"  move {m['id']:>6}  create={m.get('create_date')}  "
              f"invoice_date={m.get('invoice_date')}  ref={m.get('ref')!r}  "
              f"{(p[1] if p else '-')[:30]}  €{m.get('amount_total')}")
except Exception as e:
    print(f"  search_read FAIL: {str(e)[:300]}")

# === STEP 3: moduli installati di interesse ===
print("\n--- STEP 3: moduli installati legati a fatturapa / l10n_it ---")
try:
    mods = client._call('ir.module.module', 'search_read',
        ['|', '|',
         ('name', 'ilike', 'fatturapa'),
         ('name', 'ilike', 'l10n_it'),
         ('name', 'ilike', 'ecotel')],
        fields=['name', 'installed_version', 'latest_version', 'state',
                'write_date', 'summary'],
        order='write_date desc',
        limit=50)
    print(f"  Moduli trovati: {len(mods)}")
    for m in mods:
        print(f"  {m.get('write_date')}  {m['name']:<40}  "
              f"state={m.get('state'):<12}  installed={m.get('installed_version')}  "
              f"latest={m.get('latest_version')}")
except Exception as e:
    print(f"  search_read ir.module.module FAIL: {str(e)[:300]}")

# === STEP 4: prova fields_get cercando il modello che dichiara tipo_documento ===
print("\n--- STEP 4: chi dichiara il campo tipo_documento? (ir.model.fields) ---")
try:
    declared = client._call('ir.model.fields', 'search_read',
        [('name', '=', 'tipo_documento'),
         ('model', '=', 'fatturapa.attachment.in')],
        fields=['id', 'name', 'model', 'ttype', 'modules', 'state',
                'create_date', 'write_date'])
    print(f"  Definizioni trovate: {len(declared)}")
    for d in declared:
        print(f"  {d}")
except Exception as e:
    print(f"  search_read ir.model.fields FAIL: {str(e)[:300]}")

print("\n" + "=" * 80)
print("FINE diagnostica.")
print("=" * 80)
sys.stdout = _stdout
_logf.close()
print(f"\nOutput salvato anche in: {OUTPUT_FILE}")
