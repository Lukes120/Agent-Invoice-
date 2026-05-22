"""
POC parser PDF Enilive — estrae i blocchi "Totale carta: <17 cifre>" e
aggrega per classificazione fiscale (POOL/uso_promiscuo) usando il
mapping config/carte_enilive_mapping.py.

Test su input/enilive_005245074729506493.pdf (fattura ACQ 29506493 del 30/04/2026).

Validazione: somma imponibili-carte + voci-extra == imponibile fattura XML (3.543,08).
"""
import re
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pdfplumber
from config.carte_enilive_mapping import (
    CARTE_ENILIVE_BY_NUMERO,
    get_classificazione_carta_enilive,
)


def _to_float_it(s: str) -> float:
    """Converte numero italiano (1.234,56) in float (1234.56)."""
    s = s.replace('.', '').replace(',', '.')
    return float(s)


# Regex per "Totale carta: <17 cifre> <tot_lordo> <imponibile> <iva>"
# Esempio reale: "Totale carta: 710200185924000012 170,95 170,95 140,13 30,82"
# Nota: spesso ci sono 4 numeri perché la 1a colonna è 'voucher' (uguale a tot_lordo se nessun voucher)
RE_TOTALE_CARTA = re.compile(
    r'Totale carta:\s*(\d{17,18})\s+'
    r'([\d.,]+)\s+'          # totale lordo (= totale al netto se 0 voucher)
    r'([\d.,]+)\s+'          # totale al netto
    r'([\d.,]+)\s+'          # imponibile
    r'([\d.,]+)',            # IVA
    re.IGNORECASE,
)


def extract_carte_breakdown(pdf_path: Path):
    """Ritorna dict numero_carta -> {tot_lordo, netto, imponibile, iva}."""
    carte = {}
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ''
            full_text.append(txt)
    text = '\n'.join(full_text)

    for m in RE_TOTALE_CARTA.finditer(text):
        numero = m.group(1)
        tot_lordo = _to_float_it(m.group(2))
        netto = _to_float_it(m.group(3))
        imponibile = _to_float_it(m.group(4))
        iva = _to_float_it(m.group(5))
        carte[numero] = {
            'numero': numero,
            'totale_lordo': tot_lordo,
            'totale_netto': netto,
            'imponibile': imponibile,
            'iva': iva,
        }
    return carte, text


def aggregate_by_classe(carte):
    """Raggruppa per classificazione fiscale."""
    agg = defaultdict(lambda: {
        'n_carte': 0,
        'totale_lordo': 0.0,
        'imponibile': 0.0,
        'iva': 0.0,
        'carte': [],
    })
    senza_mappa = []
    for numero, c in carte.items():
        classif = get_classificazione_carta_enilive(numero)
        if classif is None:
            senza_mappa.append(numero)
            classif = '<NON IN MAPPA>'
        bucket = agg[classif]
        bucket['n_carte'] += 1
        bucket['totale_lordo'] += c['totale_lordo']
        bucket['imponibile'] += c['imponibile']
        bucket['iva'] += c['iva']
        bucket['carte'].append(numero)
    return dict(agg), senza_mappa


def main():
    pdf_path = ROOT / 'input' / 'enilive_005245074729506493.pdf'
    print(f'PDF: {pdf_path.name}')
    print('=' * 70)

    carte, full_text = extract_carte_breakdown(pdf_path)
    print(f'\nCarte estratte dal PDF: {len(carte)}')
    print(f'Carte in mapping (carte_enilive.xlsx): {len(CARTE_ENILIVE_BY_NUMERO)}')

    print(f'\n{"NUMERO":<22} {"TOT_LORDO":>10} {"IMPONIB.":>10} {"IVA":>8}  CLASSE')
    print('-' * 80)
    tot_lordo_sum = 0.0
    tot_imp_sum = 0.0
    tot_iva_sum = 0.0
    for numero in sorted(carte.keys()):
        c = carte[numero]
        classif = get_classificazione_carta_enilive(numero) or '⚠ NON IN MAPPA'
        print(f'{numero:<22} {c["totale_lordo"]:>10,.2f} {c["imponibile"]:>10,.2f} '
              f'{c["iva"]:>8,.2f}  {classif}')
        tot_lordo_sum += c['totale_lordo']
        tot_imp_sum += c['imponibile']
        tot_iva_sum += c['iva']

    print('-' * 80)
    print(f'{"SOMMA":<22} {tot_lordo_sum:>10,.2f} {tot_imp_sum:>10,.2f} {tot_iva_sum:>8,.2f}')

    print('\n' + '=' * 70)
    print('AGGREGAZIONE PER CLASSIFICAZIONE FISCALE')
    print('=' * 70)
    agg, senza_mappa = aggregate_by_classe(carte)
    for classif in sorted(agg.keys()):
        bucket = agg[classif]
        print(f'\n  {classif}:')
        print(f'    carte: {bucket["n_carte"]}')
        print(f'    totale lordo: €{bucket["totale_lordo"]:>10,.2f}')
        print(f'    imponibile:   €{bucket["imponibile"]:>10,.2f}')
        print(f'    IVA:          €{bucket["iva"]:>10,.2f}')

    if senza_mappa:
        print(f'\n⚠ Carte trovate nel PDF ma NON nel mapping:')
        for n in senza_mappa:
            print(f'    {n}')

    # Verifica quadratura vs XML
    XML_IMPONIBILE = 3543.08
    XML_TOTALE = 4322.56
    print('\n' + '=' * 70)
    print('QUADRATURA vs XML')
    print('=' * 70)
    delta_imp = XML_IMPONIBILE - tot_imp_sum
    delta_tot = XML_TOTALE - tot_lordo_sum
    print(f'  Imponibile XML:       €{XML_IMPONIBILE:>10,.2f}')
    print(f'  Imponibile PDF carte: €{tot_imp_sum:>10,.2f}')
    print(f'  DELTA imponibile:     €{delta_imp:>10,.2f}  (positivo = voci extra non legate a carte)')
    print()
    print(f'  Totale XML:           €{XML_TOTALE:>10,.2f}')
    print(f'  Totale PDF carte:     €{tot_lordo_sum:>10,.2f}')
    print(f'  DELTA totale:         €{delta_tot:>10,.2f}')

    # Vedo se ci sono voci extra (FEE, CANONE) nel testo per giustificare il delta
    print('\nRicerca voci extra (fuori dal blocco "Totale carta: ..."):')
    for keyword in ('FEE SICUREZZA', 'CANONE', 'ENI0027', 'COSTO ANNUO'):
        if keyword.lower() in full_text.lower():
            # Estraggo la riga
            for line in full_text.splitlines():
                if keyword.lower() in line.lower():
                    print(f'    {line.strip()}')

    # Carte nel mapping NON viste nel PDF (es. carte non usate questa fattura)
    nel_mapping = set(CARTE_ENILIVE_BY_NUMERO.keys())
    nel_pdf = set(carte.keys())
    non_usate = nel_mapping - nel_pdf
    if non_usate:
        print(f'\nCarte presenti in mapping ma NON usate in questa fattura ({len(non_usate)}):')
        for n in sorted(non_usate):
            info = CARTE_ENILIVE_BY_NUMERO[n]
            print(f'    {n}  ({info["classificazione"]})  {info["nickname"]}')


if __name__ == '__main__':
    main()
