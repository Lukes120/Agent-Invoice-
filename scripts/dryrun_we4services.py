"""
DRY-RUN del writer create_bozza_multilinea su WE4SERVICES P03696.

Testa entrambe le fatture pending del fornitore:
  - 5351769 (n.64/01 €3.559,43 Oneri Factoring, IVA 0%)
  - 5351793 (n.70/01 €9,16 FEE Maturata, IVA 22%)

Non scrive nulla. Stampa la mappatura POL libera -> riga XML risultante.
"""
import sys
import os
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.odoo_writer import OdooWriter
from core.fatturapa_parser import parse_from_base64
from config.rules import MAPPATURA_FORNITORI_FISSI


class FakeAnalysis:
    """Shim minimale per l'analysis che il writer si aspetta."""
    def __init__(self, xml_data, attachment_id, attachment_create_date=''):
        self.xml_data = xml_data
        self.attachment_id = attachment_id
        self.attachment_create_date = attachment_create_date
        self.raw_xml = ''


def run_dryrun(client, att_id, mapping):
    print()
    print('=' * 90)
    print(f'>>> ATTACHMENT {att_id}')
    print('=' * 90)

    att = client._call(
        'fatturapa.attachment.in', 'read', [att_id],
        ['id', 'datas', 'create_date'],
    )[0]
    xml_data = parse_from_base64(att['datas'])
    print(f'  N.   : {xml_data.numero}')
    print(f'  Data : {xml_data.data}')
    print(f'  Tot. : {xml_data.importo_totale}')
    print(f'  Imp. : {xml_data.imponibile_totale}')
    print(f'  Cedente: {xml_data.cedente_denominazione} ({xml_data.cedente_partita_iva})')
    print(f'  N.righe: {len(xml_data.righe)}')
    for r in xml_data.righe:
        print(f"   #{r.numero_linea} subt={r.prezzo_totale} aliq={r.aliquota_iva}% "
              f"desc={(r.descrizione or '')[:80]}")

    analysis = FakeAnalysis(
        xml_data=xml_data,
        attachment_id=att_id,
        attachment_create_date=att.get('create_date') or '',
    )

    print()
    print('=== DRY-RUN create_bozza_multilinea ===')
    writer = OdooWriter(client, dry_run=True)
    result = writer.create_bozza_multilinea(analysis, mapping)
    print()
    print('Result:')
    for k, v in result.to_dict().items():
        print(f'  {k}: {v!r}')
    return result


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    mapping = MAPPATURA_FORNITORI_FISSI['IT14861711001']
    print('Mapping caricato:')
    for k, v in mapping.items():
        print(f'  {k}: {v!r}')

    # Le 2 fatture WE4SERVICES pending
    for att_id in (5351769, 5351793):
        run_dryrun(client, att_id, mapping)

    print()
    print('=' * 90)
    print('DRY-RUN COMPLETATO. Nessuna scrittura su Odoo.')
    print('=' * 90)


if __name__ == '__main__':
    main()
