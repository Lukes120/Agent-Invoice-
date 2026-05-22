"""
Scansiona gli attachment Rema Tarlazzi NON registered, parsa XML e identifica
quello con Numero == 'V6/2026/000041080' (o contenente '41080').
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'],
    db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'],
    password=os.environ['ODOO_PASSWORD'],
)
client.connect()

# Rema Tarlazzi su Odoo: 2 partner con stessa P.IVA IT01634070435
PARTNER_IDS = [52217, 1250]
TARGET = '41080'

atts = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('xml_supplier_id', 'in', PARTNER_IDS),
     ('registered', '=', False),
     ('is_self_invoice', '=', False)],
    fields=['id', 'att_name', 'invoices_total', 'invoices_date',
            'create_date'],
    order='create_date desc',
    limit=200,
)
print(f"Attachment Rema NON registered: {len(atts)}")

found = None
for a in atts:
    full = client._call('fatturapa.attachment.in', 'read', [a['id']],
                      fields=['datas'])
    if not full or not full[0].get('datas'):
        continue
    try:
        parsed = parse_from_base64(full[0]['datas'])
    except Exception as e:
        print(f"  parse error att {a['id']}: {e}")
        continue
    num = parsed.numero or ''
    marker = '<<<' if TARGET in num else ''
    print(f"  att {a['id']:>7} | num={num!r:<30} | data={parsed.data} | "
          f"tot={parsed.importo_totale} {marker}")
    if TARGET in num:
        found = (a, parsed)

if found:
    a, parsed = found
    print()
    print('=' * 90)
    print(f">>> FATTURA TROVATA: attachment id={a['id']}")
    print('=' * 90)
    print(f"  Numero        : {parsed.numero}")
    print(f"  Data          : {parsed.data}")
    print(f"  Tipo doc      : {parsed.tipo_documento}")
    print(f"  Cedente       : {parsed.cedente_denominazione} ({parsed.cedente_partita_iva})")
    print(f"  Imponibile    : {parsed.imponibile_totale}")
    print(f"  IVA           : {parsed.imposta_totale}")
    print(f"  Totale        : {parsed.importo_totale}")
    print(f"  Causali       : {parsed.causali}")
    print(f"  OdA rif.      : {parsed.oda_riferimenti}")
    print(f"  OdA grezzi    : {parsed.oda_valori_grezzi}")
    print(f"  OdA testuali  : {parsed.oda_riferimenti_testuali}")
    print(f"  Commessa rif  : {parsed.commessa_riferimenti}")
    print(f"  Contratto rif : {parsed.contratto_riferimenti}")
    print(f"  Ricezione rif : {parsed.ricezione_riferimenti}")
    print()
    print(f"  LINEE ({len(parsed.righe)}):")
    for r in parsed.righe:
        cod = ''
        if r.codice_articolo_valore:
            cod = f" [{r.codice_articolo_tipo}={r.codice_articolo_valore}]"
        if r.codici_articolo:
            cod += f" codici={r.codici_articolo}"
        print(f"   #{r.numero_linea:>3} qty={r.quantita} pu={r.prezzo_unitario} "
              f"subt={r.prezzo_totale} aliq={r.aliquota_iva}%{cod}")
        print(f"        desc: {(r.descrizione or '')[:100]}")
        if r.riferimenti_oda:
            print(f"        rif.oda: {r.riferimenti_oda}")
        if r.altri_dati_gestionali:
            print(f"        altri: {r.altri_dati_gestionali}")
else:
    print("\nFattura V6/2026/000041080 NON trovata tra le NON-registered.")
    print("Provo a cercarla anche tra le REGISTERED...")

    atts_reg = client._call(
        'fatturapa.attachment.in', 'search_read',
        [('xml_supplier_id', 'in', PARTNER_IDS),
         ('registered', '=', True)],
        fields=['id', 'att_name', 'in_invoice_ids', 'create_date'],
        order='create_date desc',
        limit=200,
    )
    print(f"  registered: {len(atts_reg)}")
    for a in atts_reg:
        full = client._call('fatturapa.attachment.in', 'read', [a['id']],
                          fields=['datas'])
        if not full or not full[0].get('datas'):
            continue
        try:
            parsed = parse_from_base64(full[0]['datas'])
        except Exception:
            continue
        if TARGET in (parsed.numero or ''):
            print(f"  TROVATA tra registered: att={a['id']} num={parsed.numero!r} "
                  f"in_invoice_ids={a.get('in_invoice_ids')}")
            break

print('\nDONE.')
