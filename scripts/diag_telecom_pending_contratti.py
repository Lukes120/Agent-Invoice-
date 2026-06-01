"""Per le fatture Telecom pending, estrae IdContratto XML e confronta con la
mappa MAPPATURA_FORNITORI_FISSI per dire quali sono noti e quali da mappare.
Read-only.
"""
import os
import sys
import base64
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64
from config.rules import MAPPATURA_FORNITORI_FISSI

TELECOM_PIVA = 'IT00488410010'

client = OdooReadOnlyClient(
    os.environ['ODOO_URL'], os.environ['ODOO_DB'],
    os.environ['ODOO_USERNAME'], os.environ['ODOO_PASSWORD'],
)
client.connect()

# Mappa contratti noti dal config
entry = MAPPATURA_FORNITORI_FISSI.get(TELECOM_PIVA, {})
contratti_noti = entry.get('contratti', {})
print(f"--- Mappa Telecom ({TELECOM_PIVA}) ---")
print(f"Contratti mappati: {len(contratti_noti)}")
for cid, sub in contratti_noti.items():
    oda = sub.get('oda_fisso') if isinstance(sub, dict) else str(sub)
    print(f"  {cid:25s} -> {oda}")
print()

# Recupera tutte le pending Telecom (Ecotel)
atts = client.get_fatturapa_attachments(
    only_unregistered=True, exclude_self_invoice=True, company_id=1
)
telecom_atts = []
for a in atts:
    sup = a.get('xml_supplier_id')
    if sup and isinstance(sup, list) and len(sup) > 1:
        # partner_id su Odoo, non basta filtrare per nome
        pass
    # filtro a posteriori sul VAT estraendo dal nome o decodificando XML
    name = a.get('name', '') or ''
    if 'TIM' in name.upper() or 'TELECOM' in name.upper():
        telecom_atts.append(a)
    elif 'IT00488410010' in name:
        telecom_atts.append(a)

# Se il filtro per name non basta, decodifico tutti i base64 (~50 fatture, accettabile)
if len(telecom_atts) < 5:
    print("(filtro per name non basta, scansiono tutti i base64 per estrarre P.IVA cedente)")
    telecom_atts = []
    for a in atts:
        try:
            data = base64.b64decode(a.get('datas') or b'')
            if TELECOM_PIVA.encode() in data:
                telecom_atts.append(a)
        except Exception:
            pass

print(f"--- Pending Telecom Ecotel: {len(telecom_atts)} ---")
print()

# Per ognuna, estrae IdContratto + numero + importo
print(f"{'ID':>8}  {'Data Ft':10}  {'Numero':25s}  {'Totale':>10}  {'IdContratto':30s}  Stato map")
print("-" * 110)
for a in telecom_atts:
    try:
        xml_b64 = a.get('datas')
        if not xml_b64:
            print(f"{a['id']:>8}  no datas")
            continue
        ftpa = parse_from_base64(xml_b64)
        numero = ftpa.numero or '-'
        contratti = ftpa.contratto_riferimenti or []
        idc = ' | '.join(contratti) if contratti else '-'
        importo = float(a.get('invoices_total', 0) or 0)
        data_ft = (a.get('invoices_date') or '')[:10]
        first_id = contratti[0] if contratti else '-'
        if first_id == '-':
            stato = 'no IdContratto'
        elif first_id in contratti_noti:
            stato = f'MAPPATO -> {contratti_noti[first_id].get("oda_fisso") if isinstance(contratti_noti[first_id], dict) else contratti_noti[first_id]}'
        else:
            stato = 'DA MAPPARE'
        print(f"{a['id']:>8}  {data_ft:10}  {numero[:25]:25s}  {importo:>10,.2f}  {idc[:30]:30s}  {stato}")
    except Exception as e:
        print(f"{a['id']:>8}  ERROR: {type(e).__name__}: {e}")
