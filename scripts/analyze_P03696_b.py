"""
Parte B: dettaglio fatture WE4SERVICES non-registered + parsing XML linee.
"""
import sys
import os
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_fatturapa_xml

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'],
    db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'],
    password=os.environ['ODOO_PASSWORD'],
)
client.connect()

PARTNER_ID = 585  # WE4SERVICES

atts = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('registered', '=', False),
     ('is_self_invoice', '=', False),
     ('xml_supplier_id', '=', PARTNER_ID)],
    fields=['id', 'att_name', 'invoices_total', 'invoices_date',
            'invoices_number', 'create_date', 'inconsistencies'],
    order='create_date desc',
)

print("=" * 90)
print(f"FATTURE WE4SERVICES (partner_id={PARTNER_ID}) non-registered: {len(atts)}")
print("=" * 90)

for a in atts:
    print(f"\n>>> ATTACHMENT id={a['id']}")
    print(f"    file        : {a.get('att_name')}")
    print(f"    n.fattura   : {a.get('invoices_number')}")
    print(f"    data ft     : {a.get('invoices_date')}")
    print(f"    totale ft   : {a.get('invoices_total')}")
    print(f"    create_date : {a.get('create_date')}")
    if a.get('inconsistencies'):
        # encode-safe
        msg = str(a['inconsistencies']).encode('ascii', 'replace').decode('ascii')
        print(f"    [incong]    : {msg}")

    # leggi XML (base64) e parsa
    xml_b64 = client.get_fatturapa_attachment_xml(a['id'])
    if not xml_b64:
        print("    XML non disponibile")
        continue
    try:
        xml_bytes = base64.b64decode(xml_b64)
        # parse_fatturapa_xml accetta str o bytes
        parsed = parse_fatturapa_xml(xml_bytes)
    except Exception as e:
        print(f"    parse ERROR: {e}")
        continue

    print(f"    tipo doc    : {parsed.tipo_documento}")
    print(f"    cedente     : {parsed.cedente_denominazione} ({parsed.cedente_partita_iva})")
    print(f"    rif.amm.    : {parsed.cedente_riferimento_amministrazione}")
    print(f"    causali     : {parsed.causali}")
    print(f"    imponibile  : {parsed.imponibile_totale}")
    print(f"    imposta     : {parsed.imposta_totale}")
    print(f"    totale doc  : {parsed.importo_totale}")
    print(f"    oda rif.    : {parsed.oda_riferimenti} (testo:{parsed.oda_riferimenti_testuali})")
    print(f"    oda grezzi  : {parsed.oda_valori_grezzi}")
    print(f"    contratto   : {parsed.contratto_riferimenti}")
    print(f"    commessa    : {parsed.commessa_riferimenti}")
    print(f"    codice cli  : {parsed.codice_cliente}")

    print(f"\n    LINEE ({len(parsed.righe)}):")
    for r in parsed.righe:
        nome = (r.descrizione or '')[:90]
        cod = ''
        if r.codice_articolo_valore:
            cod = f" [{r.codice_articolo_tipo}={r.codice_articolo_valore}]"
        if r.codici_articolo:
            cod += f" codici={r.codici_articolo}"
        periodo = ''
        if r.data_inizio_periodo or r.data_fine_periodo:
            periodo = f" periodo={r.data_inizio_periodo}->{r.data_fine_periodo}"
        print(f"      #{r.numero_linea:>2} qty={r.quantita} pu={r.prezzo_unitario} "
              f"subt={r.prezzo_totale} aliq={r.aliquota_iva}%{cod}{periodo}")
        print(f"           desc: {nome}")
        if r.altri_dati_gestionali:
            print(f"           altri: {r.altri_dati_gestionali}")
        if r.riferimenti_oda:
            print(f"           rif.oda: {r.riferimenti_oda}")

print("\n" + "=" * 90)
print("DONE.")
