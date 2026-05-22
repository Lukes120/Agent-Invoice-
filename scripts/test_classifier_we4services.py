"""
Verifica che il classifier riconosca le fatture WE4SERVICES pending come
MAPPATURA_FORNITORE_FISSO con OdA P03696. Read-only su Odoo.
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
from core.fatturapa_analyzer import FatturaPAAnalyzer, FatturaPAAnalysis
from config.rules import MAPPATURA_FORNITORI_FISSI


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    # matcher non usato da _try_supplier_fixed_mapping → posso passare None
    analyzer = FatturaPAAnalyzer(
        client=client,
        matcher=None,
        tol_total=25.0,
        supplier_mapping=MAPPATURA_FORNITORI_FISSI,
    )

    for att_id in (5351769, 5351793):
        print()
        print('=' * 90)
        print(f'>>> ATTACHMENT {att_id}')
        print('=' * 90)
        att = client._call('fatturapa.attachment.in', 'read', [att_id],
                           ['id', 'datas', 'create_date'])[0]
        xml_data = parse_from_base64(att['datas'])
        analysis = FatturaPAAnalysis(
            attachment_id=att_id,
            attachment_name=f'att-{att_id}',
            xml_data=xml_data,
        )
        analysis.classification = 'PENDING'
        analyzer._try_supplier_fixed_mapping(analysis)
        print(f'P.IVA cedente : {xml_data.cedente_partita_iva}')
        print(f'Classificazione: {analysis.classification}')
        print(f'Suggested      :')
        for a in analysis.actions_suggested:
            print(f"   - {a}")
        for w in analysis.warnings:
            print(f"   [WARN] {w}")
        po = getattr(analysis, 'purchase_order', None)
        if po:
            print(f'OdA collegato  : {po.get("name")} state={po.get("state")} '
                  f'imp={po.get("amount_untaxed")}')


if __name__ == '__main__':
    main()
