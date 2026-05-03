"""
Entry point dell'Odoo Invoice Matching Agent.

Uso:
    python run_agent.py                          # Analisi ultimi 30 giorni
    python run_agent.py --from 2026-04-01 --to 2026-04-18
    python run_agent.py --dashboard-only         # Solo dashboard, no Excel
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from dotenv import load_dotenv

# Aggiungo la root al path per importare config e core
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.rules import (
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA, TOLLERANZA_TOTALE_FATTURA,
    ODA_PATTERNS, DEFAULT_LOOKBACK_DAYS, STATI_FATTURA_DA_ANALIZZARE,
)
from core.odoo_client import OdooReadOnlyClient
from core.matcher import InvoiceMatcher, InvoiceAnalysis
from core.classifier import InvoiceClassifier
from core.keyword_rules import classify_line_by_keyword, extract_oda_references
from reports.dashboard import generate_dashboard
from reports.excel_report import generate_excel


def setup_logging(log_dir: Path):
    """Configura logging su file + stdout."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger('agent')


def find_purchase_order_for_invoice(client: OdooReadOnlyClient,
                                    invoice: dict,
                                    logger) -> dict:
    """
    Cerca l'OdA collegato alla fattura.
    Strategia:
    1. Guarda invoice_origin (se Odoo ha già collegato l'OdA)
    2. Estrae riferimenti da ref, narration, note con pattern regex
    """
    # 1. invoice_origin diretto
    origin = invoice.get('invoice_origin')
    if origin:
        po = client.search_purchase_order_by_name(origin.strip())
        if po:
            return po

    # 2. Cerca pattern nei campi testuali
    sources = [
        invoice.get('ref', ''),
        invoice.get('narration', '') or '',
        invoice.get('name', '') or '',
    ]
    for src in sources:
        if not src:
            continue
        refs = extract_oda_references(src, ODA_PATTERNS)
        for ref in refs:
            po = client.search_purchase_order_by_name(ref)
            if po:
                logger.debug(f"OdA {ref} trovato da testo '{src[:40]}'")
                return po

    return None


def analyze_invoice(client: OdooReadOnlyClient,
                    invoice: dict,
                    matcher: InvoiceMatcher,
                    classifier: InvoiceClassifier,
                    logger) -> InvoiceAnalysis:
    """Analizza una singola fattura."""
    analysis = InvoiceAnalysis(invoice=invoice)

    try:
        # Recupero righe fattura
        line_ids = invoice.get('invoice_line_ids', [])
        inv_lines = client.get_invoice_lines(line_ids)

        # Cerco OdA
        po = find_purchase_order_for_invoice(client, invoice, logger)
        analysis.purchase_order = po

        po_lines = []
        if po:
            po_lines = client.get_purchase_order_lines(po.get('order_line', []))

        # Match riga per riga
        for il in inv_lines:
            lm = matcher.match_line(il, po_lines)
            analysis.line_matches.append(lm)

        # Classificazione finale
        classifier.classify(analysis)

    except Exception as e:
        logger.error(f"Errore analisi fattura {invoice.get('name')}: {e}")
        analysis.classification = "ANOMALIA"
        analysis.warnings.append(str(e))

    return analysis


def main():
    parser = argparse.ArgumentParser(description="Odoo Invoice Matching Agent")
    parser.add_argument('--from', dest='date_from', help='Data inizio (YYYY-MM-DD)')
    parser.add_argument('--to', dest='date_to', help='Data fine (YYYY-MM-DD)')
    parser.add_argument('--dashboard-only', action='store_true',
                        help='Genera solo la dashboard, non il report Excel')
    args = parser.parse_args()

    root = Path(__file__).parent
    log_dir = root / 'logs'
    output_root = root / 'output'
    logger = setup_logging(log_dir)

    # Carico credenziali
    env_file = root / 'config' / 'credentials.env'
    if not env_file.exists():
        logger.error(
            f"File credenziali non trovato: {env_file}\n"
            f"Copia credentials.env.template in credentials.env e compila i dati."
        )
        sys.exit(1)
    load_dotenv(env_file)

    odoo_url = os.getenv('ODOO_URL')
    odoo_db = os.getenv('ODOO_DB')
    odoo_user = os.getenv('ODOO_USERNAME')
    odoo_pwd = os.getenv('ODOO_PASSWORD')

    if not all([odoo_url, odoo_db, odoo_user, odoo_pwd]):
        logger.error("Credenziali Odoo incomplete nel file credentials.env")
        sys.exit(1)

    # Range date
    if args.date_to:
        date_to = args.date_to
    else:
        date_to = date.today().strftime('%Y-%m-%d')

    if args.date_from:
        date_from = args.date_from
    else:
        date_from = (date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    period = f"{date_from} -> {date_to}"
    logger.info(f"=== Odoo Invoice Matching Agent ===")
    logger.info(f"Periodo analisi: {period}")

    # Connessione Odoo
    logger.info(f"Connessione a {odoo_url} / db={odoo_db}")
    client = OdooReadOnlyClient(odoo_url, odoo_db, odoo_user, odoo_pwd)
    client.connect()

    # Recupero fatture
    logger.info(f"Recupero fatture fornitore in stato {STATI_FATTURA_DA_ANALIZZARE}...")
    invoices = client.get_vendor_bills(date_from, date_to, STATI_FATTURA_DA_ANALIZZARE)
    logger.info(f"Trovate {len(invoices)} fatture da analizzare")

    # Inizializzo matcher e classifier
    matcher = InvoiceMatcher(
        TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA,
        TOLLERANZA_TOTALE_FATTURA, classify_line_by_keyword,
    )
    classifier = InvoiceClassifier()

    # Analisi
    analyses = []
    for i, inv in enumerate(invoices, 1):
        logger.info(f"[{i}/{len(invoices)}] Analizzo {inv.get('name')} - {inv.get('partner_id')}")
        analysis = analyze_invoice(client, inv, matcher, classifier, logger)
        analyses.append(analysis)

    # Riepilogo su log
    from collections import Counter
    summary = Counter(a.classification for a in analyses)
    logger.info("=== Riepilogo ===")
    for cls, count in summary.most_common():
        logger.info(f"  {cls}: {count}")

    # Output
    output_dir = output_root / datetime.now().strftime('%Y-%m-%d')
    output_dir.mkdir(parents=True, exist_ok=True)

    dashboard_path = output_dir / 'dashboard.html'
    generate_dashboard(analyses, str(dashboard_path), period)
    logger.info(f"Dashboard generata: {dashboard_path}")

    if not args.dashboard_only:
        excel_path = output_dir / 'report_dettagliato.xlsx'
        generate_excel(analyses, str(excel_path))
        logger.info(f"Excel generato: {excel_path}")

    logger.info("=== Fine elaborazione ===")


if __name__ == '__main__':
    main()
