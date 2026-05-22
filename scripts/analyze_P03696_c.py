"""
Parte C: ispezione raw byte dei 2 attachment WE4SERVICES non-registered.
Le incongruenze indicano cedenti diversi (PALMA, HILTI) -> verifica diretta.
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

ATT_IDS = [5351793, 5351769]

for aid in ATT_IDS:
    print(f"\n=== ATTACHMENT id={aid} ===")
    xml_b64 = client.get_fatturapa_attachment_xml(aid)
    if not xml_b64:
        print("   xml vuoto")
        continue
    raw = base64.b64decode(xml_b64)
    print(f"   raw size : {len(raw)} bytes")
    print(f"   head bytes (hex): {raw[:16].hex()}")
    print(f"   head ascii      : {raw[:200]!r}")
    # Se inizia con <?xml o < e' XML plain
    is_xml = raw.lstrip().startswith(b'<')
    print(f"   is XML plain    : {is_xml}")

    if is_xml:
        try:
            txt = raw.decode('utf-8', errors='replace')
        except Exception as e:
            print(f"   decode error: {e}")
            txt = raw.decode('latin-1', errors='replace')
        # cerca tag chiave
        import re
        m = re.search(r'<Denominazione>([^<]+)</Denominazione>', txt)
        if m:
            print(f"   Denominazione   : {m.group(1)}")
        m = re.search(r'<IdCodice>([^<]+)</IdCodice>', txt)
        if m:
            print(f"   IdCodice (PIVA) : {m.group(1)}")
        m = re.search(r'<TipoDocumento>([^<]+)</TipoDocumento>', txt)
        if m:
            print(f"   TipoDocumento   : {m.group(1)}")
        m = re.search(r'<Numero>([^<]+)</Numero>', txt)
        if m:
            print(f"   Numero          : {m.group(1)}")
        m = re.search(r'<Data>([^<]+)</Data>', txt)
        if m:
            print(f"   Data            : {m.group(1)}")
        m = re.search(r'<ImportoTotaleDocumento>([^<]+)</ImportoTotaleDocumento>', txt)
        if m:
            print(f"   Totale          : {m.group(1)}")
        # IdDocumento (OdA, contratto, convenzione)
        for tag in ('IdDocumento', 'CodiceCommessaConvenzione',
                    'RiferimentoAmministrazione', 'Causale'):
            for mm in re.finditer(r'<' + tag + r'>([^<]+)</' + tag + r'>', txt):
                print(f"   {tag:<25}: {mm.group(1)}")
        # Tutte le righe (DettaglioLinee)
        linee = re.findall(
            r'<DettaglioLinee>(.+?)</DettaglioLinee>', txt, re.DOTALL)
        print(f"   N. DettaglioLinee: {len(linee)}")
        for i, lin in enumerate(linee, 1):
            desc = re.search(r'<Descrizione>([^<]+)</Descrizione>', lin)
            pu = re.search(r'<PrezzoUnitario>([^<]+)</PrezzoUnitario>', lin)
            pt = re.search(r'<PrezzoTotale>([^<]+)</PrezzoTotale>', lin)
            iv = re.search(r'<AliquotaIVA>([^<]+)</AliquotaIVA>', lin)
            print(f"     riga#{i}: pu={pu.group(1) if pu else '-'} "
                  f"pt={pt.group(1) if pt else '-'} "
                  f"aliq={iv.group(1) if iv else '-'}")
            print(f"        desc: {(desc.group(1) if desc else '-')[:100]}")

        # provo anche il parser ufficiale (passandogli str non bytes)
        print("\n   -- parse_fatturapa_xml su str --")
        parsed = parse_fatturapa_xml(txt)
        print(f"   parser: cedente='{parsed.cedente_denominazione}' "
              f"piva={parsed.cedente_partita_iva} "
              f"tipo={parsed.tipo_documento} totale={parsed.importo_totale} "
              f"linee={len(parsed.righe)}")
        if parsed.parsing_errors:
            print(f"   parsing_errors  : {parsed.parsing_errors}")
print("\nDONE.")
