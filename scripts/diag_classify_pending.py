"""Classifica readonly tutte le e-fatture in coda (fatturapa.attachment.in pending)
come farebbe l'agent dalla webapp, ma da terminale e senza salvare nulla.

Replica la pipeline di Agent Invoices/app.py:266-340 con stessi parametri.
Nessuna scrittura su Odoo. Nessuna scrittura sul DB dashboard.

Uso:
    python scripts/diag_classify_pending.py                  # Ecotel (default)
    python scripts/diag_classify_pending.py --company 2      # altra company
    python scripts/diag_classify_pending.py --limit 10
    python scripts/diag_classify_pending.py --csv            # salva anche CSV in output/
"""
import os
import sys
import csv
import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.matcher import InvoiceMatcher
from core.fatturapa_analyzer import FatturaPAAnalyzer
from core.keyword_rules import classify_line_by_keyword
from config.rules import (
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA, TOLLERANZA_TOTALE_FATTURA,
    TOLLERANZA_MATCH_IMPLICITO_PERCENT, TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
    TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA, TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
    MATCH_IMPLICITO_GUARDIA_DUPLICATI, MATCH_IMPLICITO_ATTIVO,
    MATCH_PARZIALE_ATTIVO, MATCH_PARZIALE_MAX_RIGHE,
    MATCH_PARZIALE_MAX_EXTRA_PERCENT, MATCH_PARZIALE_TOLLERANZA_ASSOLUTA,
    SUGGERIMENTI_ATTIVI, SUGGERIMENTI_MAX_RIGHE,
    SUGGERIMENTI_TOLLERANZA_ASSOLUTA, SUGGERIMENTI_MAX_AGE_MONTHS,
    MAPPATURA_FORNITORI_FISSI, MAPPATURA_FORNITORI_FISSI_ATTIVA,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--company', type=int, default=1, help='company_id Odoo (1=Ecotel)')
    ap.add_argument('--limit', type=int, default=None, help='max fatture da analizzare')
    ap.add_argument('--csv', action='store_true', help='salva anche CSV in output/')
    args = ap.parse_args()

    # Connessione Odoo
    client = OdooReadOnlyClient(
        os.environ['ODOO_URL'], os.environ['ODOO_DB'],
        os.environ['ODOO_USERNAME'], os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    # Recupero allegati pending
    attachments = client.get_fatturapa_attachments(
        only_unregistered=True,
        exclude_self_invoice=True,
        company_id=args.company,
        limit=args.limit,
    )
    n = len(attachments)
    print(f"Trovati {n} allegati pending per company_id={args.company}")
    print()
    if not n:
        return

    # Matcher + Analyzer (parametri identici a Agent Invoices/app.py:284-302)
    matcher = InvoiceMatcher(
        TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA,
        TOLLERANZA_TOTALE_FATTURA, classify_line_by_keyword,
    )
    analyzer = FatturaPAAnalyzer(
        client, matcher, TOLLERANZA_TOTALE_FATTURA,
        implicit_match_enabled=MATCH_IMPLICITO_ATTIVO,
        implicit_match_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_PERCENT,
        implicit_match_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
        implicit_match_loose_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA,
        implicit_match_loose_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
        implicit_match_duplicate_guard=MATCH_IMPLICITO_GUARDIA_DUPLICATI,
        partial_match_enabled=MATCH_PARZIALE_ATTIVO,
        partial_match_max_rows=MATCH_PARZIALE_MAX_RIGHE,
        partial_match_max_extra_percent=MATCH_PARZIALE_MAX_EXTRA_PERCENT,
        partial_match_tolerance_absolute=MATCH_PARZIALE_TOLLERANZA_ASSOLUTA,
        suggestions_enabled=SUGGERIMENTI_ATTIVI,
        suggestions_max_lines=SUGGERIMENTI_MAX_RIGHE,
        suggestions_tolerance_absolute=SUGGERIMENTI_TOLLERANZA_ASSOLUTA,
        suggestions_max_age_months=SUGGERIMENTI_MAX_AGE_MONTHS,
        supplier_mapping_enabled=MAPPATURA_FORNITORI_FISSI_ATTIVA,
        supplier_mapping=MAPPATURA_FORNITORI_FISSI,
    )

    # Analisi
    analyses = []
    for i, att in enumerate(attachments, 1):
        try:
            a = analyzer.analyze(att)
        except Exception as e:
            class _Err:
                attachment_id = att.get('id')
                attachment_name = att.get('name', '')
                supplier_name = (att.get('xml_supplier_id') or [None, '-'])[1] if att.get('xml_supplier_id') else '-'
                invoice_total = float(att.get('invoices_total', 0) or 0)
                invoice_date = att.get('invoices_date') or ''
                attachment_create_date = str(att.get('create_date') or '')
                classification = 'ERRORE_ANALISI'
                purchase_order = None
                warnings = [f"{type(e).__name__}: {e}"]
            a = _Err()
        analyses.append(a)
        print(f"  [{i:3d}/{n}] {a.attachment_name[:30]:30s} -> {a.classification}")

    # Post-processing run-level (stesso ordine di app.py)
    analyzer.apply_duplicate_guard(analyses)
    analyzer.apply_strict_wins_over_loose(analyses)
    analyzer.apply_run_cumulative_check(analyses)

    # Output tabellare
    print()
    print("=" * 130)
    print(f"{'ID':>6}  {'DataRicSdI':<10}  {'DataFt':<10}  {'Fornitore':<35}  {'Totale':>11}  {'OdA':<10}  Classificazione")
    print("-" * 130)
    for a in analyses:
        oda = '-'
        po = getattr(a, 'purchase_order', None)
        if po and isinstance(po, dict):
            oda = po.get('name', '-')
        data_ric = (a.attachment_create_date or '')[:10]
        data_ft = (a.invoice_date or '')[:10]
        print(
            f"{a.attachment_id:>6}  {data_ric:<10}  {data_ft:<10}  "
            f"{(a.supplier_name or '-')[:35]:<35}  {a.invoice_total:>10,.2f}  "
            f"{oda[:10]:<10}  {a.classification}"
        )
    print("=" * 130)

    # Riepilogo Counter
    counter = Counter(a.classification for a in analyses)
    print()
    print("=== Riepilogo classificazione ===")
    for cls, cnt in counter.most_common():
        print(f"  {cls:<35s}: {cnt}")
    print(f"  {'-'*35}---")
    print(f"  {'TOTALE':<35s}: {sum(counter.values())}")

    # CSV opzionale
    if args.csv:
        out_dir = ROOT / 'output'
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d-%H%M')
        csv_path = out_dir / f'pending_company{args.company}_{ts}.csv'
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['attachment_id', 'data_ricezione_sdi', 'data_fattura',
                        'fornitore', 'totale', 'oda', 'classificazione', 'warnings'])
            for a in analyses:
                po = getattr(a, 'purchase_order', None)
                oda = po.get('name', '') if (po and isinstance(po, dict)) else ''
                warns = ' | '.join(getattr(a, 'warnings', []) or [])
                w.writerow([
                    a.attachment_id,
                    (a.attachment_create_date or '')[:10],
                    (a.invoice_date or '')[:10],
                    a.supplier_name or '',
                    f'{a.invoice_total:.2f}',
                    oda,
                    a.classification,
                    warns,
                ])
        print()
        print(f"CSV salvato: {csv_path}")


if __name__ == '__main__':
    main()
