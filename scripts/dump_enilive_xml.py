"""
Dump strutturale del XML Enilive pending per capire dove stanno le carte.
"""
import sys
import os
import base64
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')
from core.odoo_client import OdooReadOnlyClient


def strip_ns(tag):
    if '}' in tag:
        return tag.split('}', 1)[1]
    return tag


def walk_tree(elem, depth=0, max_depth=12, limit_per_level=999):
    """Pretty-print di un nodo XML con tutti i tag e testi (troncati)."""
    if depth > max_depth:
        return
    tag = strip_ns(elem.tag)
    text = (elem.text or '').strip()
    text_short = text[:120] + ('…' if len(text) > 120 else '')
    indent = '  ' * depth
    if text:
        print(f'{indent}<{tag}> {text_short!r}')
    else:
        print(f'{indent}<{tag}>')
    for c in list(elem)[:limit_per_level]:
        walk_tree(c, depth + 1, max_depth, limit_per_level)


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    att = client._call(
        'fatturapa.attachment.in', 'read',
        [5351804],
        ['id', 'name', 'datas'],
    )[0]
    b64 = att['datas']
    raw = base64.b64decode(b64)
    print(f'XML size: {len(raw):,} bytes')

    # Salvo copia locale per ispezioni successive
    out_path = ROOT / 'input' / att['name']
    out_path.write_bytes(raw)
    print(f'Saved to: {out_path}')

    root = ET.fromstring(raw)

    # Sommario top-level
    print('\n=== TOP-LEVEL ===')
    for c in list(root):
        print(f'  {strip_ns(c.tag)}')

    # FatturaElettronicaBody (contiene righe)
    print('\n=== FatturaElettronicaBody/DatiBeniServizi/DettaglioLinee (prime 2 righe COMPLETE) ===')
    bodies = [c for c in root.iter() if strip_ns(c.tag) == 'DettaglioLinee']
    print(f'Trovate {len(bodies)} DettaglioLinee nel documento')
    for i, dl in enumerate(bodies[:2]):
        print(f'\n--- DettaglioLinee #{i+1} ---')
        walk_tree(dl)

    # Allegati?
    print('\n=== ALLEGATI ===')
    allegati = [c for c in root.iter() if strip_ns(c.tag) == 'Allegati']
    print(f'Trovati {len(allegati)} blocchi <Allegati>')
    for i, a in enumerate(allegati[:3]):
        print(f'\n--- Allegato #{i+1} ---')
        for child in a:
            t = strip_ns(child.tag)
            if t == 'Attachment':
                size = len(child.text or '')
                print(f'  <Attachment> base64 size={size:,}')
            else:
                txt = (child.text or '').strip()[:200]
                print(f'  <{t}> {txt!r}')

    # AltriDatiGestionali in fattura/intestazione (potrebbe avere indice carte)
    print('\n=== AltriDatiGestionali (top-level) ===')
    adg_top = []
    for c in root.iter():
        if strip_ns(c.tag) == 'AltriDatiGestionali':
            # solo quelli NON dentro DettaglioLinee
            adg_top.append(c)
    print(f'Trovati {len(adg_top)} blocchi AltriDatiGestionali totali')
    # Mostro distribuzione TipoDato per i primi 50
    from collections import Counter
    tipi = Counter()
    for adg in adg_top:
        td = None
        for ch in adg:
            if strip_ns(ch.tag) == 'TipoDato':
                td = (ch.text or '').strip()
                break
        if td:
            tipi[td] += 1
    print(f'Distribuzione TipoDato: {dict(tipi)}')


if __name__ == '__main__':
    main()
