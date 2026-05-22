"""
Analisi puntuale fattura Rema Tarlazzi V6/2026/000041080 in
fatturapa.attachment.in. Read-only.
"""
import sys
import os
import base64
import re
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

FT_NUMBER = 'V6/2026/000041080'

# 1. Cerca attachment per numero fattura
print(f"Ricerca attachment con invoices_number ilike '{FT_NUMBER}'...")
atts = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('invoices_number', 'ilike', FT_NUMBER)],
    fields=['id', 'name', 'att_name', 'xml_supplier_id',
            'invoices_total', 'invoices_date', 'invoices_number',
            'registered', 'is_self_invoice', 'in_invoice_ids',
            'company_id', 'create_date', 'inconsistencies'],
    order='create_date desc',
    limit=10,
)
print(f"Trovati: {len(atts)}")

if not atts:
    # Prova senza il prefisso V6/2026/ se l'XML mette numero "puro"
    short = FT_NUMBER.split('/')[-1]
    print(f"Riprovo con tail '{short}'...")
    atts = client._call(
        'fatturapa.attachment.in', 'search_read',
        [('invoices_number', 'ilike', short)],
        fields=['id', 'name', 'att_name', 'xml_supplier_id',
                'invoices_total', 'invoices_date', 'invoices_number',
                'registered', 'is_self_invoice', 'in_invoice_ids',
                'company_id', 'create_date'],
        order='create_date desc',
        limit=20,
    )
    print(f"Trovati: {len(atts)}")

# Filtra cedente Rema se ce ne sono multipli
candidates = []
for a in atts:
    sup = a.get('xml_supplier_id')
    sup_name = sup[1] if sup else ''
    if 'rema' in sup_name.lower() or 'tarlazzi' in sup_name.lower():
        candidates.append(a)
print(f"Match cedente Rema/Tarlazzi: {len(candidates)}")

if not candidates:
    # Mostra comunque tutti i candidati per indagine
    candidates = atts

for a in candidates:
    print()
    print('=' * 90)
    print(f"ATTACHMENT id={a['id']}")
    print('=' * 90)
    for k in ('att_name', 'invoices_number', 'invoices_date',
              'invoices_total', 'registered', 'is_self_invoice',
              'create_date', 'in_invoice_ids'):
        print(f"  {k}: {a.get(k)}")
    sup = a.get('xml_supplier_id')
    co = a.get('company_id')
    print(f"  cedente : {sup[1] if sup else '-'} (id={sup[0] if sup else '-'})")
    print(f"  company : {co[1] if co else '-'} (id={co[0] if co else '-'})")
    if a.get('inconsistencies'):
        msg = str(a['inconsistencies']).encode('ascii', 'replace').decode('ascii')
        print(f"  [incong]: {msg}")

    # Leggi e parsifica XML
    full = client._call('fatturapa.attachment.in', 'read', [a['id']],
                      fields=['datas'])
    if not full or not full[0].get('datas'):
        print("  XML non disponibile")
        continue
    try:
        parsed = parse_from_base64(full[0]['datas'])
    except Exception as e:
        print(f"  parse error: {e}")
        continue

    print()
    print(f"  Tipo doc       : {parsed.tipo_documento}")
    print(f"  Numero         : {parsed.numero}")
    print(f"  Data           : {parsed.data}")
    print(f"  Cedente        : {parsed.cedente_denominazione} ({parsed.cedente_partita_iva})")
    print(f"  Cessionario rif: {parsed.cedente_riferimento_amministrazione}")
    print(f"  Imponibile     : {parsed.imponibile_totale}")
    print(f"  IVA            : {parsed.imposta_totale}")
    print(f"  Totale         : {parsed.importo_totale}")
    print(f"  Causali        : {parsed.causali}")
    print(f"  OdA riferimenti (puliti) : {parsed.oda_riferimenti}")
    print(f"  OdA riferimenti (grezzi) : {parsed.oda_valori_grezzi}")
    print(f"  OdA da descrizioni       : {parsed.oda_riferimenti_testuali}")
    print(f"  Commessa rif.            : {parsed.commessa_riferimenti}")
    print(f"  Contratto rif.           : {parsed.contratto_riferimenti}")
    print(f"  Ricezione rif.           : {parsed.ricezione_riferimenti}")
    print(f"  Codice cliente           : {parsed.codice_cliente}")

    print()
    print(f"  LINEE ({len(parsed.righe)}):")
    for r in parsed.righe:
        cod = ''
        if r.codice_articolo_valore:
            cod = f" [{r.codice_articolo_tipo}={r.codice_articolo_valore}]"
        print(f"   #{r.numero_linea:>3} qty={r.quantita} pu={r.prezzo_unitario} "
              f"subt={r.prezzo_totale} aliq={r.aliquota_iva}%{cod}")
        print(f"        desc: {(r.descrizione or '')[:100]}")
        if r.riferimenti_oda:
            print(f"        rif.oda: {r.riferimenti_oda}")
        if r.altri_dati_gestionali:
            print(f"        altri: {r.altri_dati_gestionali}")

print()
print('DONE.')
