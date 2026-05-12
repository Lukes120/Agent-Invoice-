"""
Analizza i PDF in input/ e confronta col dataset noto (cHECK Q1 2026).
Mostra:
- Apparati estratti per ogni PDF
- Confronto: nuovi vs gia conosciuti vs spariti
- Eventuali differenze di layout (totale fattura presente / parsing errori)
"""
import os
import sys
import re
import glob
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.pdf_parser import parse_pdf_autostrade
from config.apparati_mapping import get_classificazione, get_apparato_info

INPUT_DIR = os.path.join(ROOT, 'input')
ROOT_PDF_NOTI = r"C:\Users\lranalletta\Downloads\cHECK\_extracted"


def analizza_dir(directory, etichetta):
    pdfs = sorted(glob.glob(os.path.join(directory, '**', '*.pdf'), recursive=True))
    print(f'{etichetta}: {len(pdfs)} PDF')
    apparati_visti = defaultdict(lambda: {'tot_eur': 0.0, 'count_pdf': 0,
                                           'count_movimenti': 0,
                                           'pdfs': []})
    summary_per_pdf = []
    for pdf in pdfs:
        rel = pdf.replace(directory + os.sep, '')
        data = parse_pdf_autostrade(pdf)
        n_app = len(data.apparati)
        s = data.somma_importi_apparati
        tot_pdf = data.totale_fattura_iva_inclusa
        summary_per_pdf.append({
            'pdf': rel, 'n_apparati': n_app,
            'somma_apparati': s,
            'totale_fattura_pdf': tot_pdf,
            'errori': data.parsing_errors,
        })
        for a in data.apparati:
            key = (a.tipo, a.apparato_id)
            d = apparati_visti[key]
            d['tot_eur'] += a.importo_iva_inclusa
            d['count_pdf'] += 1
            d['count_movimenti'] += a.n_movimenti
            d['pdfs'].append(os.path.basename(pdf))
    return apparati_visti, summary_per_pdf


# 1) PDF noti (Autostrade Q1)
noti_autostrade = sorted(glob.glob(
    os.path.join(ROOT_PDF_NOTI, '**', 'Autostrade*.pdf'), recursive=True))
noti_apcoa = sorted(glob.glob(
    os.path.join(ROOT_PDF_NOTI, '**', 'Apcoa*.pdf'), recursive=True))
noti_set = set()
for p in noti_autostrade + noti_apcoa:
    d = parse_pdf_autostrade(p)
    for a in d.apparati:
        noti_set.add((a.tipo, a.apparato_id))
print(f'Apparati distinti noti dal dataset cHECK Q1 (Autostrade+Apcoa): {len(noti_set)}\n')

# 2) PDF in input/
nuovi_visti, summary = analizza_dir(INPUT_DIR, 'PDF in input/')
print()

# 3) Per ogni PDF input/: dettaglio
print('=' * 100)
for s in summary:
    print(f'\nFile: {s["pdf"]}')
    print(f'  Apparati estratti: {s["n_apparati"]}')
    print(f'  Somma totali apparati (IVA incl.): {s["somma_apparati"]}')
    print(f'  Totale fattura header PDF: {s["totale_fattura_pdf"]}')
    if s['errori']:
        print('  Parsing warnings:')
        for w in s['errori'][:5]:
            print(f'    - {w}')

# 4) Confronto con noti
print()
print('=' * 100)
print(f'CONFRONTO: apparati distinti nei 3 PDF input = {len(nuovi_visti)}')
print('=' * 100)

nuovi = []
gia_noti = []
for key, info in nuovi_visti.items():
    if key in noti_set:
        gia_noti.append((key, info))
    else:
        nuovi.append((key, info))

print(f'\nApparati GIA visti nel dataset Q1: {len(gia_noti)}')
print(f'Apparati NUOVI (mai visti in Q1): {len(nuovi)}')

# nuovi: dettaglio
if nuovi:
    print()
    print('-' * 100)
    print('APPARATI NUOVI (compaiono solo in input/ aprile, non in Q1):')
    print('-' * 100)
    print(f'{"Tipo":<10} {"ID":<22} {"Stato mappa":<14} {"€ tot":>10} {"# mov":>6}  Esempio PDF')
    for (tipo, aid), info in sorted(nuovi, key=lambda kv: -kv[1]['tot_eur']):
        stato = get_classificazione(aid) or 'DA CENSIRE'
        sample = info['pdfs'][0] if info['pdfs'] else ''
        print(f'{tipo:<10} {aid:<22} {stato:<14} {info["tot_eur"]:>10.2f} '
              f'{info["count_movimenti"]:>6}  {sample}')

# gia noti: dettaglio
if gia_noti:
    print()
    print('-' * 100)
    print('APPARATI GIA NOTI (con stato mappa):')
    print('-' * 100)
    print(f'{"Tipo":<10} {"ID":<22} {"Stato mappa":<14} {"€ in input":>10} {"# mov":>6}')
    mappati = 0
    da_censire = 0
    for (tipo, aid), info in sorted(gia_noti, key=lambda kv: -kv[1]['tot_eur']):
        stato = get_classificazione(aid)
        if stato:
            mappati += 1
        else:
            da_censire += 1
        print(f'{tipo:<10} {aid:<22} {(stato or "DA CENSIRE"):<14} '
              f'{info["tot_eur"]:>10.2f} {info["count_movimenti"]:>6}')
    print(f'\n  di cui MAPPATI: {mappati} / DA CENSIRE: {da_censire}')

# 5) Apparati Q1 che NON appaiono in input (= dismessi o non fatturati ad aprile)
spariti = [k for k in noti_set if k not in nuovi_visti]
print()
print('-' * 100)
print(f'APPARATI Q1 NON FATTURATI nei PDF input/ aprile (eventuali dismissioni / inattivi mese): {len(spariti)}')
print('-' * 100)
for tipo, aid in sorted(spariti):
    info = get_apparato_info(aid)
    descr = f'{info.get("targa","")} {info.get("veicolo_descrizione","")[:40]}' if info else 'NON IN MAPPA'
    print(f'  {tipo:<10} {aid:<22}  -> {descr}')

# 6) Riepilogo finale
print()
print('=' * 100)
print('RIEPILOGO FINALE')
print('=' * 100)
tot_eur_input = sum(i['tot_eur'] for i in nuovi_visti.values())
tot_mappato = sum(i['tot_eur'] for k, i in nuovi_visti.items()
                   if get_classificazione(k[1]))
tot_da_censire = tot_eur_input - tot_mappato
print(f'Apparati distinti in input/: {len(nuovi_visti)}')
print(f'  Mappati: {sum(1 for k in nuovi_visti if get_classificazione(k[1]))}')
print(f'  Da censire: {sum(1 for k in nuovi_visti if not get_classificazione(k[1]))}')
print(f'Importo totale IVA-incl. dei 3 PDF: € {tot_eur_input:.2f}')
print(f'  Mappato: € {tot_mappato:.2f}')
print(f'  Da censire: € {tot_da_censire:.2f}')
print(f'\nNuovi apparati che servirebbero in PARCO AUTO: {len(nuovi)}')
