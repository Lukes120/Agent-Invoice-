"""
Audit: per ogni PDF Autostrade in Downloads/cHECK, lista gli apparati che
NON sono ne in APPARATI_MAP ne in APPARATI_ALIAS, con importo IVA-incl.
Aiuta a capire cosa manca nel file PARCO AUTO 2026.
"""
import os, sys, glob
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.pdf_parser import parse_pdf_autostrade
from config.apparati_mapping import (
    APPARATI_MAP, APPARATI_ALIAS, get_classificazione, normalize_apparato_lookup
)

ROOT_PDF = r"C:\Users\lranalletta\Downloads\cHECK\_extracted"

pdfs = sorted(glob.glob(os.path.join(ROOT_PDF, '**', 'Autostrade*.pdf'), recursive=True))
print(f'PDF Autostrade trovati: {len(pdfs)}\n')

agg_missing = defaultdict(lambda: {'count_pdf': 0, 'tot_eur': 0.0, 'tipo': '', 'sample_pdf': ''})
totals_per_pdf = []

for pdf in pdfs:
    data = parse_pdf_autostrade(pdf)
    rel = pdf.replace(ROOT_PDF + os.sep, '')
    n_app = len(data.apparati)
    tot_eur = data.somma_importi_apparati
    n_mapped = sum(1 for a in data.apparati if get_classificazione(a.apparato_id))
    n_missing = n_app - n_mapped
    eur_missing = sum(a.importo_iva_inclusa
                      for a in data.apparati
                      if not get_classificazione(a.apparato_id))
    totals_per_pdf.append({
        'pdf': rel, 'apparati': n_app, 'tot_eur': tot_eur,
        'mapped': n_mapped, 'missing': n_missing, 'eur_missing': eur_missing,
    })
    for a in data.apparati:
        if not get_classificazione(a.apparato_id):
            k = (a.tipo, a.apparato_id)
            agg_missing[k]['count_pdf'] += 1
            agg_missing[k]['tot_eur'] += a.importo_iva_inclusa
            agg_missing[k]['tipo'] = a.tipo
            if not agg_missing[k]['sample_pdf']:
                agg_missing[k]['sample_pdf'] = rel

# Riepilogo per PDF
print('=' * 100)
print(f'{"PDF":<70} {"App":>4} {"€ tot":>9} {"Map":>4} {"Mis":>4} {"€ mis":>9}')
print('=' * 100)
for r in totals_per_pdf:
    short = '/'.join(r['pdf'].split(os.sep)[-2:])
    print(f'{short[:70]:<70} {r["apparati"]:>4} {r["tot_eur"]:>9.2f} '
          f'{r["mapped"]:>4} {r["missing"]:>4} {r["eur_missing"]:>9.2f}')

tot_apparati = sum(r['apparati'] for r in totals_per_pdf)
tot_mapped = sum(r['mapped'] for r in totals_per_pdf)
tot_missing = sum(r['missing'] for r in totals_per_pdf)
tot_eur_all = sum(r['tot_eur'] for r in totals_per_pdf)
tot_eur_miss = sum(r['eur_missing'] for r in totals_per_pdf)
print('-' * 100)
print(f'{"TOTALE su tutti i PDF":<70} {tot_apparati:>4} {tot_eur_all:>9.2f} '
      f'{tot_mapped:>4} {tot_missing:>4} {tot_eur_miss:>9.2f}')
if tot_eur_all > 0:
    print(f'\n  Quota mappata: {100*tot_mapped/max(1,tot_apparati):.1f}% per numero, '
          f'{100*(1-tot_eur_miss/tot_eur_all):.1f}% per valore')

# Apparati unici mancanti (consolidati)
print()
print('=' * 100)
print(f'APPARATI UNICI MANCANTI (raggruppati): {len(agg_missing)}')
print('=' * 100)
print(f'{"Tipo":<10} {"ID":<22} {"#PDF":>5} {"€ totale":>10}  Esempio PDF')
print('-' * 100)
items = sorted(agg_missing.items(), key=lambda kv: -kv[1]['tot_eur'])
for (tipo, aid), info in items:
    sample = info['sample_pdf'].split(os.sep)[-1]
    print(f'{tipo:<10} {aid:<22} {info["count_pdf"]:>5} {info["tot_eur"]:>10.2f}  {sample[:50]}')
