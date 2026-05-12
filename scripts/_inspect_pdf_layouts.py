"""
Ispeziona il layout testuale di un PDF Telepass e un PDF Apcoa per capire
come estrarre gli apparati (ID + totale per apparato).
"""
import os, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pdfplumber

ROOT_PDF = r"C:\Users\lranalletta\Downloads\cHECK\_extracted"

# 1 Telepass + 1 Apcoa
samples = [
    sorted(glob.glob(os.path.join(ROOT_PDF, '**', 'Telepass*.pdf'),
                     recursive=True))[0],
    sorted(glob.glob(os.path.join(ROOT_PDF, '**', 'Apcoa*.pdf'),
                     recursive=True))[0],
]

for pdf_path in samples:
    print('=' * 100)
    print(f'PDF: {pdf_path}')
    print('=' * 100)
    with pdfplumber.open(pdf_path) as pdf:
        print(f'Pagine: {len(pdf.pages)}')
        for pi, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            print(f'\n----- PAGINA {pi+1} -----')
            for ln in text.split('\n'):
                print(ln)
            if pi >= 2:  # max 3 pagine
                break
    print()
