"""
Verifica RiferimentoAmministrazione su fatture Enilive:
1. Fattura pending specifica (filename '202605090288706079205478aa')
2. Ultimi N attachment Enilive registered (campione storico)

Per ogni fattura: % righe con riferimento_amministrazione popolato, primi 3 valori,
sample di descrizione riga (per capire se in fallback serve estrarre numero carta dalla desc).
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
from core.fatturapa_parser import parse_from_base64

PARTITA_IVA_ENILIVE = 'IT11403240960'
TARGET_FILENAME_HINT = '202605090288706079205478aa'


def inspect_attachment(client, att):
    """Scarica datas, parsa XML, stampa diagnosi RiferimentoAmministrazione."""
    full = client._call(
        'fatturapa.attachment.in', 'read',
        [att['id']],
        ['id', 'name', 'datas', 'registered', 'is_self_invoice',
         'company_id', 'create_date'],
    )
    if not full:
        print(f'  ⚠ Impossibile leggere attachment {att["id"]}')
        return
    rec = full[0]
    print(f'\n--- ATT id={rec["id"]} ---')
    print(f'  name:        {rec.get("name")!r}')
    print(f'  registered:  {rec.get("registered")}')
    print(f'  company:     {rec.get("company_id")}')
    print(f'  create_date: {rec.get("create_date")}')
    b64 = rec.get('datas')
    if not b64:
        print('  ⚠ campo datas vuoto')
        return
    try:
        data = parse_from_base64(b64)
    except Exception as e:
        print(f'  ⚠ parsing fallito: {e}')
        return
    print(f'  cedente:     {data.cedente_denominazione!r}  ({data.cedente_partita_iva})')
    print(f'  numero/data: {data.numero}  {data.data}')
    print(f'  totali:      imponibile {data.imponibile_totale}  totale {data.importo_totale}')
    n_righe = len(data.righe)
    con_rifamm = [l for l in data.righe if l.riferimento_amministrazione]
    senza_rifamm = [l for l in data.righe if not l.riferimento_amministrazione]
    print(f'  righe totali: {n_righe}')
    print(f'  righe con RiferimentoAmministrazione: {len(con_rifamm)} '
          f'({100*len(con_rifamm)/n_righe:.0f}% se n>0)' if n_righe else '')
    if con_rifamm:
        print(f'  primi 3 RiferimentoAmministrazione:')
        for l in con_rifamm[:3]:
            print(f'    "{l.riferimento_amministrazione}" -> desc: {l.descrizione[:80]!r}')
    if senza_rifamm:
        print(f'  esempio righe SENZA RiferimentoAmministrazione (max 3):')
        for l in senza_rifamm[:3]:
            print(f'    desc: {l.descrizione[:100]!r}  prezzo={l.prezzo_totale}')


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    print('=' * 70)
    print('1) Fattura pending — search filename hint')
    print('=' * 70)
    fn_filter = [
        '|',
        ['name', 'ilike', TARGET_FILENAME_HINT],
        ['name', 'ilike', 'ZQbqv'],
    ]
    pending = client._call(
        'fatturapa.attachment.in', 'search_read',
        fn_filter,
        ['id', 'name', 'registered', 'company_id', 'create_date'],
        limit=5,
    )
    print(f'\nTrovati {len(pending)} attachment col filename hint')
    for att in pending:
        inspect_attachment(client, att)

    print('\n' + '=' * 70)
    print('2) Ultimi 5 attachment Enilive (registered=True), tutti su Ecotel')
    print('=' * 70)
    storico = client._call(
        'fatturapa.attachment.in', 'search_read',
        [
            ['cedente_partita_iva', '=', PARTITA_IVA_ENILIVE],
            ['registered', '=', True],
            ['company_id', '=', 1],
        ],
        ['id', 'name', 'registered', 'company_id', 'create_date'],
        order='create_date desc',
        limit=5,
    )
    print(f'\nTrovati {len(storico)} attachment storici Enilive')
    for att in storico:
        inspect_attachment(client, att)

    print('\n' + '=' * 70)
    print('FINE')
    print('=' * 70)


if __name__ == '__main__':
    main()
