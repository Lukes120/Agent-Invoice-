"""
Entry point Fase 1 - Odoo Invoice Matching Agent.
Analizza gli allegati fatturapa.attachment.in non ancora registrati,
estrae OdA dall'XML, esegue matching, produce dashboard + Excel.

Uso:
    python run_agent_fase1.py                      # tutte le non-registrate
    python run_agent_fase1.py --limit 50           # prime 50 (per test)
    python run_agent_fase1.py --from 2026-04-01    # solo dal 1/4
    python run_agent_fase1.py --dashboard-only
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import Counter
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.rules import (
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA, TOLLERANZA_TOTALE_FATTURA,
    MATCH_IMPLICITO_ATTIVO, TOLLERANZA_MATCH_IMPLICITO_PERCENT,
    TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
    TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA,
    TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
    MATCH_IMPLICITO_GUARDIA_DUPLICATI,
    MATCH_PARZIALE_ATTIVO, MATCH_PARZIALE_MAX_RIGHE,
    MATCH_PARZIALE_MAX_EXTRA_PERCENT, MATCH_PARZIALE_TOLLERANZA_ASSOLUTA,
    SUGGERIMENTI_ATTIVI, SUGGERIMENTI_MAX_RIGHE,
    SUGGERIMENTI_TOLLERANZA_ASSOLUTA,
    SUGGERIMENTI_MAX_AGE_MONTHS,
    MAPPATURA_FORNITORI_FISSI, MAPPATURA_FORNITORI_FISSI_ATTIVA,
)
from core.odoo_client import OdooReadOnlyClient
from core.matcher import InvoiceMatcher
from core.keyword_rules import classify_line_by_keyword
from core.fatturapa_analyzer import FatturaPAAnalyzer, FatturaPAAnalysis
from reports.dashboard_fase1 import generate_dashboard_fase1
from reports.excel_report_fase1 import generate_excel_fase1


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"fase1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger('agent_fase1')


def main():
    parser = argparse.ArgumentParser(
        description="Odoo Invoice Matching Agent - Fase 1 (solo OdA)"
    )
    parser.add_argument('--from', dest='date_from',
                        help='Data inizio ricezione XML (YYYY-MM-DD)')
    parser.add_argument('--to', dest='date_to',
                        help='Data fine ricezione XML (YYYY-MM-DD)')
    parser.add_argument('--limit', type=int, default=None,
                        help='Numero massimo di allegati da analizzare')
    parser.add_argument('--dashboard-only', action='store_true')
    parser.add_argument('--include-self-invoice', action='store_true',
                        help='Includi anche le autofatture (reverse charge). '
                             'Default: escluse come fa Odoo con filtro "Da registrare"')
    parser.add_argument('--all-companies', action='store_true',
                        help='Analizza tutte le aziende. Default: solo company_id della env')
    args = parser.parse_args()

    root = Path(__file__).parent
    logger = setup_logging(root / 'logs')

    # Credenziali
    env_file = root / 'config' / 'credentials.env'
    if not env_file.exists():
        logger.error(f"File credentials.env mancante in {env_file}")
        sys.exit(1)
    load_dotenv(env_file)

    odoo_url = os.getenv('ODOO_URL')
    odoo_db = os.getenv('ODOO_DB')
    odoo_user = os.getenv('ODOO_USERNAME')
    odoo_pwd = os.getenv('ODOO_PASSWORD')
    odoo_company = int(os.getenv('ODOO_COMPANY_ID', '1'))

    if not all([odoo_url, odoo_db, odoo_user, odoo_pwd]):
        logger.error("Credenziali Odoo incomplete")
        sys.exit(1)

    logger.info("=== Odoo Invoice Matching Agent - FASE 1 ===")
    logger.info(f"Connessione a {odoo_url} / db={odoo_db}")
    client = OdooReadOnlyClient(odoo_url, odoo_db, odoo_user, odoo_pwd)
    client.connect()

    # Parametri filtro (replicano "Da registrare" di Odoo)
    exclude_self = not args.include_self_invoice
    company_filter = None if args.all_companies else odoo_company

    # Descrizione del periodo
    date_from = args.date_from
    date_to = args.date_to
    if date_from or date_to:
        period = f"allegati ricevuti {date_from or 'inizio'} -> {date_to or 'oggi'}"
    else:
        period = "fatture 'Da registrare'"
    if exclude_self:
        period += " (escluse autofatture)"
    if company_filter:
        period += f" (company_id={company_filter})"
    if args.limit:
        period += f" [limitato a {args.limit}]"

    logger.info(f"Recupero: {period}")
    attachments = client.get_fatturapa_attachments(
        only_unregistered=True,
        exclude_self_invoice=exclude_self,
        company_id=company_filter,
        date_from=date_from,
        date_to=date_to,
        limit=args.limit,
    )
    logger.info(f"Trovati {len(attachments)} allegati da analizzare")

    # Matcher + Analyzer
    matcher = InvoiceMatcher(
        tol_percent=TOLLERANZA_PERCENTUALE,
        tol_absolute=TOLLERANZA_ASSOLUTA,
        tol_total=TOLLERANZA_TOTALE_FATTURA,
        keyword_classifier=classify_line_by_keyword,
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

    analyses = []
    for i, att in enumerate(attachments, 1):
        supplier = att.get('xml_supplier_id')
        sup_name = supplier[1] if isinstance(supplier, list) and len(supplier) > 1 else 'N/D'
        logger.info(f"[{i}/{len(attachments)}] {att.get('name', '?')[:60]} | {sup_name}")
        try:
            a = analyzer.analyze(att)
            analyses.append(a)
        except Exception as e:
            logger.error(f"Errore analisi: {e}")

    # Post-processing: guardia duplicati per match implicito
    logger.info("Applicazione guardia duplicati match implicito...")
    analyzer.apply_duplicate_guard(analyses)

    # Post-processing: guardia "stretto vince su largo" per match impliciti
    logger.info("Applicazione guardia strict-wins-over-loose...")
    analyzer.apply_strict_wins_over_loose(analyses)

    # Post-processing: guardia cumulativa di run su OdA espliciti
    logger.info("Applicazione guardia cumulativa di run...")
    analyzer.apply_run_cumulative_check(analyses)

    # Riepilogo
    summary = Counter(a.classification for a in analyses)
    logger.info("=== Riepilogo classificazioni ===")
    for cls, count in summary.most_common():
        pct = count / len(analyses) * 100 if analyses else 0
        logger.info(f"  {cls}: {count} ({pct:.1f}%)")

    # Output
    output_dir = root / 'output' / datetime.now().strftime('%Y-%m-%d_%H%M')
    output_dir.mkdir(parents=True, exist_ok=True)

    dashboard_path = output_dir / 'dashboard.html'
    generate_dashboard_fase1(analyses, str(dashboard_path), period)
    logger.info(f"Dashboard: {dashboard_path}")

    if not args.dashboard_only:
        excel_path = output_dir / 'report_fase1.xlsx'
        generate_excel_fase1(analyses, str(excel_path))
        logger.info(f"Excel: {excel_path}")

    logger.info("=== Fine elaborazione Fase 1 ===")


if __name__ == '__main__':
    main()
