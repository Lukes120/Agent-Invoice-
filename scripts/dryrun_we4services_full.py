"""
Ispezione FULL delle assignments che il writer produrrebbe per le 2 fatture
WE4SERVICES (senza limite di troncamento sui log).
"""
import sys
import os
import logging
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.odoo_writer import OdooWriter
from core.fatturapa_parser import parse_from_base64
from config.rules import MAPPATURA_FORNITORI_FISSI


class FakeAnalysis:
    def __init__(self, xml_data, attachment_id, attachment_create_date=''):
        self.xml_data = xml_data
        self.attachment_id = attachment_id
        self.attachment_create_date = attachment_create_date
        self.raw_xml = ''


# Intercetto le chiamate write/create per stampare i vals prima del DRY-RUN
captured = []

def fake_call(model, method, *args, **kwargs):
    # solo per riferimento; in DRY-RUN il writer non chiama davvero write/create
    captured.append((model, method, args, kwargs))


client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'],
    db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'],
    password=os.environ['ODOO_PASSWORD'],
)
client.connect()

mapping = MAPPATURA_FORNITORI_FISSI['IT14861711001']

for att_id in (5351769, 5351793):
    print()
    print('=' * 90)
    print(f'>>> ATTACHMENT {att_id}')
    print('=' * 90)
    att = client._call('fatturapa.attachment.in', 'read', [att_id],
                      ['id', 'datas', 'create_date'])[0]
    xml_data = parse_from_base64(att['datas'])
    analysis = FakeAnalysis(xml_data=xml_data, attachment_id=att_id,
                            attachment_create_date=att.get('create_date') or '')

    # Eseguo il writer in DRY-RUN, ma uso una sottoclasse che cattura assignments
    class InspectingWriter(OdooWriter):
        def _validate_mapping(self, m):
            return None
    writer = InspectingWriter(client, dry_run=True)

    # Monkey-patch del logger.info per non troncare e catturare
    import core.odoo_writer as ow
    captured_logs = []
    orig = ow.logger.info
    def my_info(msg, *a, **kw):
        captured_logs.append(str(msg))
    ow.logger.info = my_info
    try:
        result = writer.create_bozza_multilinea(analysis, mapping)
    finally:
        ow.logger.info = orig

    print(f'\nFattura: {xml_data.numero} | data={xml_data.data} | tot={xml_data.importo_totale}')
    print(f'Success: {result.success} | po_line_id={result.po_line_id}')
    print(f'Old name (raw): {result.old_name!r}')
    print(f'Old price     : {result.old_price_unit}')
    if result.error_message:
        print(f'Errore: {result.error_message}')

    print('\nAssignments (full):')
    for line in captured_logs:
        if 'DRY_RUN' in line and 'PO line' in line:
            print(f'  {line}')

# Tx il logging.info NON troncato manualmente — ricostruisco descrizione
# come farebbe il writer per spiegare meglio l'output.
print()
print('=' * 90)
print('RICOSTRUZIONE description ATTESA (keep_original_with_ref):')
print('=' * 90)
casi = [
    (5351769, '64/01', 73954, 'Oneri Factoring // Addebito Oneri Factoring Maggio'),
    (5351793, '70/01', 73970, 'FEE // FEE Maturata su Vostra fattura Cessione Maggio'),
]
for att_id, ft_num, pol_id, old_name in casi:
    description = f"{old_name} (rif.ft {ft_num})"
    print(f"  ATT {att_id} -> POL {pol_id}")
    print(f"    description = {description!r}")
