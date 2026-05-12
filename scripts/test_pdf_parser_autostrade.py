"""
Test del PDF parser Autostrade su 2-3 PDF reali Q1 2026.
Verifica:
1. Estrazione apparati (TELEPASS / VIACARD)
2. Somma totali apparati ≈ totale fattura
3. Lookup classificazione su APPARATI_MAP
4. Apparati non mappati (warning)

Read-only: legge solo i PDF e la mappa Python.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.pdf_parser import parse_pdf_autostrade, calcola_split_furgoni_promiscuo
from config.apparati_mapping import APPARATI_MAP, get_classificazione

PDFS = [
    # marzo cc 261713569 — la "fattura test" del piano v3
    r"C:\Users\lranalletta\Downloads\cHECK\_extracted\1 - Contratto 261713569\03 - Fatture Marzo 2026\Autostrade ft 000000002099556D del 30.03.2026.pdf",
    # febbraio cc 261713569
    r"C:\Users\lranalletta\Downloads\cHECK\_extracted\1 - Contratto 261713569\02 - Fatture Febbraio 2026\Autostrade ft 000000001333245D del 28.02.2026.pdf",
    # gennaio cc 217718183 (altro contratto, sample diverso)
    r"C:\Users\lranalletta\Downloads\cHECK\_extracted\2 - Contratto 217718183\01 - Fatture Gennaio 2026\Autostrade ft 000000000568311D del 31.01.2026.pdf",
]

print(f"Mappa apparati caricata: {len(APPARATI_MAP)} entries")
print()


def test_pdf(path):
    print('=' * 80)
    print(f'PDF: {os.path.basename(path)}')
    print('=' * 80)
    if not os.path.exists(path):
        print(f'  FILE NON TROVATO')
        return
    data = parse_pdf_autostrade(path)
    print(f'Totale fattura (header PDF): {data.totale_fattura_iva_inclusa}')
    print(f'Apparati estratti: {len(data.apparati)}')
    print(f'Somma totali apparati (IVA incl.): {data.somma_importi_apparati}')
    if data.totale_fattura_iva_inclusa:
        diff = round(data.somma_importi_apparati - data.totale_fattura_iva_inclusa, 2)
        print(f'Differenza somma_apparati - totale_fattura: {diff}')

    if data.parsing_errors:
        print('Parsing warnings:')
        for w in data.parsing_errors:
            print(f'  - {w}')

    print()
    print('Apparati estratti dettaglio:')
    print(f'{"Tipo":<10} {"ID":<20} {"Mov":>5} {"Importo":>10} {"Classif":<15} {"Targa":<10}')
    print('-' * 80)
    mapped = 0
    not_mapped = 0
    for a in sorted(data.apparati, key=lambda x: -x.importo_iva_inclusa):
        cls = get_classificazione(a.apparato_id) or '???'
        if cls in ('furgoni', 'uso_promiscuo'):
            mapped += 1
        else:
            not_mapped += 1
        targa = APPARATI_MAP.get(a.apparato_id, {}).get('targa', '')
        print(f'{a.tipo:<10} {a.apparato_id:<20} {a.n_movimenti:>5} '
              f'{a.importo_iva_inclusa:>10.2f} {cls:<15} {targa:<10}')

    print()
    print(f'Mappati: {mapped} / Non mappati: {not_mapped}')

    # Esempio split usando un imponibile fittizio = IVA-incl / 1.22
    # (in produzione l'imponibile arriva dall'XML)
    if data.somma_importi_apparati > 0:
        imp_xml_stimato = round(data.somma_importi_apparati / 1.22, 2)
        split = calcola_split_furgoni_promiscuo(
            data, imp_xml_stimato, get_classificazione)
        print()
        print(f'Split simulato (imponibile stimato {imp_xml_stimato}):')
        print(f'  Furgoni (420160 100%):    EUR {split["imponibile_furgoni"]}')
        print(f'  Uso promiscuo (420840 70%): EUR {split["imponibile_promiscuo"]}')
        print(f'  Apparati furgoni: {len(split["apparati_furgoni"])}')
        print(f'  Apparati promiscuo: {len(split["apparati_promiscuo"])}')
        print(f'  Apparati non mappati: {len(split["apparati_non_mappati"])}')
        for w in split.get('warnings', []):
            print(f'  WARN: {w}')
    print()


for p in PDFS:
    test_pdf(p)
