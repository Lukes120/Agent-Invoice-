"""Diagnostico Wind Tre: per le fatture pending estrae TUTTI i campi di routing
(RiferimentoAmministrazione cedente, contratto_riferimenti, oda grezzi, righe)
e confronta con i contratti mappati. Read-only.
"""
import io
import os
import sys
import base64
from pathlib import Path
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64
from config.rules import MAPPATURA_FORNITORI_FISSI, resolve_mapping_entry

WINDTRE_PIVA = 'IT13378520152'

client = OdooReadOnlyClient(
    os.environ['ODOO_URL'], os.environ['ODOO_DB'],
    os.environ['ODOO_USERNAME'], os.environ['ODOO_PASSWORD'],
)
client.connect()

entry = MAPPATURA_FORNITORI_FISSI.get(WINDTRE_PIVA, {})
contratti_noti = entry.get('contratti', {})
print("=== Contratti Wind Tre MAPPATI ===")
for cid, sub in contratti_noti.items():
    print(f"  {cid:20s} -> {sub.get('oda_fisso')}")
print()

atts = client.get_fatturapa_attachments(
    only_unregistered=True, exclude_self_invoice=True, company_id=1
)

wt = []
for a in atts:
    try:
        ftpa = parse_from_base64(a.get('datas'))
        if (getattr(ftpa, 'cedente_partita_iva', '') or '').upper() == WINDTRE_PIVA:
            wt.append((a, ftpa))
    except Exception:
        pass

print(f"=== Pending Wind Tre Ecotel: {len(wt)} ===\n")

for a, ftpa in wt:
    rif_amm = getattr(ftpa, 'cedente_riferimento_amministrazione', '') or '-'
    contratti = getattr(ftpa, 'contratto_riferimenti', []) or []
    oda_grezzi = getattr(ftpa, 'oda_valori_grezzi', []) or []
    ricez = getattr(ftpa, 'ricezione_riferimenti', []) or []
    resolved = resolve_mapping_entry(entry, ftpa)

    print(f"--- Att {a['id']} | Ft {ftpa.numero} | {(a.get('invoices_date') or '')[:10]} "
          f"| Totale {float(a.get('invoices_total', 0) or 0):,.2f} ---")
    print(f"  RiferimentoAmministrazione (cedente): {rif_amm!r}")
    print(f"  contratto_riferimenti (DatiContratto): {contratti}")
    print(f"  oda_valori_grezzi:                     {oda_grezzi}")
    print(f"  ricezione_riferimenti:                 {ricez}")
    if resolved:
        print(f"  >>> RISOLTO su contratto {resolved.get('contratto_id')} -> OdA {resolved.get('oda_fisso')}")
    else:
        print(f"  >>> NESSUN contratto mappato corrisponde -> finisce in NO_ODA")
    # righe
    print(f"  Righe ({len(ftpa.righe)}):")
    for ln in ftpa.righe[:12]:
        ra = getattr(ln, 'riferimento_amministrazione', '') or ''
        desc = (ln.descrizione or '')[:60]
        print(f"    [{ln.numero_linea}] {desc:60s} {ln.prezzo_totale:>10,.2f}  rifAmmRiga={ra!r}")
    print()
