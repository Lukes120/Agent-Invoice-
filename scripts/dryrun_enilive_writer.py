"""
Test DRY-RUN end-to-end del writer create_bozza_enilive su fattura pending
(attachment 5351804, ACQ 29506493 del 30/04/2026).

Non scrive nulla. Stampa quello che farebbe.
"""
import sys
import os
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.odoo_writer import OdooWriter
from core.fatturapa_parser import parse_from_base64
from config.rules import MAPPATURA_FORNITORI_FISSI


class FakeAnalysis:
    """Mini-shim per analysis (basta che abbia xml_data + raw_xml + attachment_id)."""
    def __init__(self, xml_data, raw_xml, attachment_id):
        self.xml_data = xml_data
        self.raw_xml = raw_xml
        self.attachment_id = attachment_id


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    # Recupero l'attachment pending
    ATT_ID = 5351804
    att = client._call(
        'fatturapa.attachment.in', 'read', [ATT_ID],
        ['id', 'name', 'datas'],
    )[0]
    raw_bytes = base64.b64decode(att['datas'])
    raw_xml = raw_bytes.decode('utf-8')

    xml_data = parse_from_base64(att['datas'])
    print(f'Fattura: {xml_data.numero}  data {xml_data.data}  totale {xml_data.importo_totale}')
    print(f'Cedente: {xml_data.cedente_denominazione} ({xml_data.cedente_partita_iva})')

    analysis = FakeAnalysis(xml_data=xml_data, raw_xml=raw_xml, attachment_id=ATT_ID)
    mapping = MAPPATURA_FORNITORI_FISSI['IT11403240960']

    print()
    print('=== DRY-RUN writer.create_bozza_enilive ===')
    writer = OdooWriter(client, dry_run=True)
    result = writer.create_bozza_enilive(analysis, mapping)
    print()
    print('Result:')
    for k, v in result.to_dict().items():
        print(f'  {k}: {v!r}')


if __name__ == '__main__':
    main()
