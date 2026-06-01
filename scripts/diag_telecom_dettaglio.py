"""Estrae dettagli leggibili dalle fatture Telecom pending: servizi, numeri di
telefono, indirizzi, periodo, importi per riga. Read-only.

Per ogni IdContratto nuovo, dovrebbe emergere abbastanza contesto da capire
se si tratta di un nuovo OdA dedicato o di un'estensione di un OdA esistente.
"""
import os
import sys
import base64
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

TELECOM_PIVA = 'IT00488410010'


def strip_ns(tag):
    """Rimuove namespace XML per facilità di accesso."""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def find_text(elem, path):
    """Cerca un sub-elemento per path (lista di tag) ignorando namespace."""
    if elem is None:
        return None
    cur = elem
    for tag in path:
        found = None
        for child in cur:
            if strip_ns(child.tag) == tag:
                found = child
                break
        if found is None:
            return None
        cur = found
    return (cur.text or '').strip() if cur.text else None


def find_all(elem, tag_name):
    """Trova tutti i sub-elementi con un certo tag (un solo livello)."""
    return [c for c in elem if strip_ns(c.tag) == tag_name] if elem is not None else []


def deep_find_all(elem, tag_name):
    """Trova ricorsivamente."""
    out = []
    for c in elem.iter():
        if strip_ns(c.tag) == tag_name:
            out.append(c)
    return out


def parse_xml(b64):
    raw = base64.b64decode(b64)
    return ET.fromstring(raw)


def estrai_dettagli_telecom(root):
    """Ritorna dict con i campi chiave."""
    info = {
        'numero': None,
        'data': None,
        'importo': None,
        'id_contratto': None,
        'causali': [],
        'rif_amministrazione': None,
        'sede_cliente': None,
        'periodo': None,
        'numeri_telefono': set(),
        'servizi': [],   # lista di (descrizione, prezzo)
    }
    # Naviga FatturaElettronica/FatturaElettronicaHeader, FatturaElettronicaBody...
    # Body è ripetuto se la fattura ha più documenti, ma per Telecom sempre 1
    body = None
    for c in root.iter():
        if strip_ns(c.tag) == 'FatturaElettronicaBody':
            body = c
            break
    if body is None:
        return info

    # Dati Generali
    info['numero'] = find_text(body, ['DatiGenerali', 'DatiGeneraliDocumento', 'Numero'])
    info['data'] = find_text(body, ['DatiGenerali', 'DatiGeneraliDocumento', 'Data'])
    info['importo'] = find_text(body, ['DatiGenerali', 'DatiGeneraliDocumento', 'ImportoTotaleDocumento'])

    # Causale (può essere multipla)
    dgd = None
    for c in body.iter():
        if strip_ns(c.tag) == 'DatiGeneraliDocumento':
            dgd = c
            break
    if dgd is not None:
        for c in dgd:
            if strip_ns(c.tag) == 'Causale' and c.text:
                info['causali'].append(c.text.strip())

    # DatiContratto.IdDocumento
    for dc in deep_find_all(body, 'DatiContratto'):
        idd = find_text(dc, ['IdDocumento'])
        if idd:
            info['id_contratto'] = idd
            break

    # Riferimento amministrazione (a livello header del cessionario)
    rif_amm = None
    for c in root.iter():
        if strip_ns(c.tag) == 'CessionarioCommittente':
            rif_amm = find_text(c, ['RiferimentoAmministrazione'])
            sede = None
            for s in c:
                if strip_ns(s.tag) == 'Sede':
                    parts = []
                    for p in ['Indirizzo', 'NumeroCivico', 'CAP', 'Comune', 'Provincia']:
                        v = find_text(s, [p])
                        if v:
                            parts.append(v)
                    info['sede_cliente'] = ', '.join(parts) or None
                    break
            break
    info['rif_amministrazione'] = rif_amm

    # Dati cessione - dati DDT / periodo dal Causale o AltriDatiGestionali
    # Cerco pattern "periodo XXX-XXX" o date nelle descrizioni
    for c in info['causali']:
        m = re.search(r'(?i)periodo[:\s]*([\w/\-\s]+\d{2,4})', c)
        if m:
            info['periodo'] = m.group(1).strip()
            break

    # Dettaglio linee
    for dl in deep_find_all(body, 'DettaglioLinee'):
        desc = find_text(dl, ['Descrizione']) or ''
        prezzo = find_text(dl, ['PrezzoTotale']) or find_text(dl, ['PrezzoUnitario']) or ''
        info['servizi'].append((desc, prezzo))
        # Estrai numeri telefono / linea da descrizione
        # Pattern italiani: cellulare (3xx 7-8 cifre), fisso (0xx 6-9 cifre)
        for m in re.finditer(r'(?<!\d)(3\d{2}[\s\.\-]?\d{6,7}|0\d{1,3}[\s\.\-]?\d{5,8})', desc):
            n = re.sub(r'\D', '', m.group(0))
            if 8 <= len(n) <= 11:
                info['numeri_telefono'].add(n)

    return info


def main():
    client = OdooReadOnlyClient(
        os.environ['ODOO_URL'], os.environ['ODOO_DB'],
        os.environ['ODOO_USERNAME'], os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    atts = client.get_fatturapa_attachments(
        only_unregistered=True, exclude_self_invoice=True, company_id=1
    )
    # Filtro: cerca P.IVA Telecom direttamente nel base64 dopo decodifica string
    telecom = []
    for a in atts:
        b64 = a.get('datas')
        if not b64:
            continue
        if isinstance(b64, bytes):
            b64_str = b64.decode('ascii', errors='ignore')
        else:
            b64_str = str(b64)
        try:
            raw = base64.b64decode(b64_str)
            if TELECOM_PIVA.encode() in raw or b'00488410010' in raw:
                telecom.append(a)
        except Exception as e:
            print(f"[{a['id']}] decode err: {e}")

    print(f"Trovate {len(telecom)} fatture Telecom pending Ecotel.")
    print("=" * 100)

    for a in telecom:
        try:
            root = parse_xml(a['datas'])
            info = estrai_dettagli_telecom(root)
        except Exception as e:
            print(f"\n[{a['id']}] ERROR: {type(e).__name__}: {e}")
            continue

        print(f"\n[attachment {a['id']}]  Numero fattura: {info['numero']}  Data: {info['data']}  Totale: € {info['importo']}")
        print(f"  IdContratto:           {info['id_contratto']}")
        print(f"  Rif. amministrazione:  {info['rif_amministrazione']}")
        print(f"  Sede cliente:          {info['sede_cliente']}")
        if info['periodo']:
            print(f"  Periodo:               {info['periodo']}")
        if info['causali']:
            print(f"  Causali ({len(info['causali'])}):")
            for c in info['causali'][:6]:
                print(f"    - {c[:120]}")
            if len(info['causali']) > 6:
                print(f"    ... (+{len(info['causali'])-6})")
        if info['numeri_telefono']:
            print(f"  Numeri telefono/linea ({len(info['numeri_telefono'])}):")
            for n in sorted(info['numeri_telefono'])[:10]:
                # Formattazione leggibile
                if n.startswith('3') and len(n) >= 10:
                    pretty = f"{n[:3]} {n[3:]}"
                elif n.startswith('0'):
                    pretty = f"{n[:3]} {n[3:]}"
                else:
                    pretty = n
                print(f"    - {pretty}")
            if len(info['numeri_telefono']) > 10:
                print(f"    ... (+{len(info['numeri_telefono'])-10})")
        if info['servizi']:
            print(f"  Servizi/righe ({len(info['servizi'])}, prime 8):")
            for desc, prezzo in info['servizi'][:8]:
                d = desc.replace('\n', ' ').strip()[:100]
                print(f"    € {prezzo:>10s}  {d}")
            if len(info['servizi']) > 8:
                print(f"    ... (+{len(info['servizi'])-8} righe)")
        print("-" * 100)


if __name__ == '__main__':
    main()
