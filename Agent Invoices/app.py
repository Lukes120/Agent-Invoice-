"""
Dashboard Flask per Odoo Invoice Agent.
Interfaccia web locale per gestione e visualizzazione dell'agent.

Lancio:
    python webapp/app.py

Poi apri il browser all'indirizzo:
    http://localhost:5000
"""

import os
import re
import sys
import json
import sqlite3
import subprocess
import threading
import queue
from pathlib import Path
from datetime import datetime
from typing import Optional, Any
from flask import (
    Flask, render_template, jsonify, request, send_file,
    Response, stream_with_context, abort
)

# Setup path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
# Carico credenziali Odoo da .env
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.matcher import InvoiceMatcher
from core.keyword_rules import classify_line_by_keyword
from core.fatturapa_analyzer import FatturaPAAnalyzer
from config.rules import (
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA,
    TOLLERANZA_TOTALE_FATTURA,
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

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.config['OUTPUT_DIR'] = ROOT / 'output'
app.config['LOGS_DIR'] = ROOT / 'logs'
app.config['DB_PATH'] = ROOT / 'webapp' / 'dashboard.db'
# Credenziali Odoo (lette da env)
app.config['ODOO_URL'] = os.getenv('ODOO_URL')
app.config['ODOO_DB'] = os.getenv('ODOO_DB')
app.config['ODOO_USERNAME'] = os.getenv('ODOO_USERNAME')
app.config['ODOO_PASSWORD'] = os.getenv('ODOO_PASSWORD')

# Filtro log werkzeug: silenzia GET /health (probe locale ogni secondo)
import logging as _logging
class _SilenceHealthFilter(_logging.Filter):
    def filter(self, record):
        msg = record.getMessage() if record.args else str(record.msg)
        return '/health' not in msg
_logging.getLogger('werkzeug').addFilter(_SilenceHealthFilter())


# Coda per log streaming durante esecuzione agent
log_queue = queue.Queue()
is_running = False
_run_lock = threading.Lock()


# Endpoint health: smette di restituire 404 (qualcosa pinga ogni secondo)
@app.route('/health')
def _health():
    return jsonify({'status': 'ok'})


# ============================================================
# DATABASE (SQLite) per storico esecuzioni e cache analisi
# ============================================================

def init_db():
    """Crea/aggiorna schema SQLite."""
    app.config['DB_PATH'].parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(app.config['DB_PATH'])
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            total_analyzed INTEGER,
            auto_validabili INTEGER,
            match_implicito INTEGER DEFAULT 0,
            match_implicito_ambiguo INTEGER DEFAULT 0,
            match_parziale INTEGER DEFAULT 0,
            match_parziale_ambiguo INTEGER DEFAULT 0,
            parziali_cumul INTEGER,
            trasporto_ok INTEGER,
            da_verificare INTEGER,
            cumul_eccede INTEGER,
            oda_non_trovato INTEGER,
            no_oda INTEGER,
            commesse INTEGER,
            anomalie INTEGER,
            total_amount REAL,
            output_dir TEXT,
            status TEXT
        )
    """)
    # Migrazione schema: aggiungi colonne se mancano
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()}
    for col in ['match_implicito', 'match_implicito_ambiguo',
                'match_parziale', 'match_parziale_ambiguo',
                'no_oda_con_suggerimenti',
                'match_da_suggerimento',
                'match_da_suggerimento_extra',
                'mappatura_fornitore',
                'mappatura_automezzi']:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} INTEGER DEFAULT 0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            attachment_id INTEGER,
            attachment_name TEXT,
            supplier_name TEXT,
            supplier_vat TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            invoice_total REAL,
            oda_xml TEXT,
            oda_odoo TEXT,
            classification TEXT,
            total_diff REAL,
            total_diff_percent REAL,
            cumulative_others REAL,
            cumulative_count INTEGER,
            commesse TEXT,
            actions TEXT,
            warnings TEXT,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_analyses_run ON analyses(run_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_analyses_class ON analyses(classification)")

    # Tabella odoo_writes: audit trail delle scritture su Odoo
    c.execute("""
        CREATE TABLE IF NOT EXISTS odoo_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER,
            timestamp TEXT,
            action TEXT,
            success INTEGER,
            move_id INTEGER,
            po_line_id INTEGER,
            old_price_unit REAL,
            old_name TEXT,
            old_date_planned TEXT,
            error_message TEXT,
            dry_run INTEGER,
            FOREIGN KEY(analysis_id) REFERENCES analyses(id)
        )
    """)
    # Migrazione analyses: aggiungi tipo_documento se manca
    existing_analysis_cols = {row[1] for row in c.execute("PRAGMA table_info(analyses)").fetchall()}
    if 'tipo_documento' not in existing_analysis_cols:
        c.execute("ALTER TABLE analyses ADD COLUMN tipo_documento TEXT")

    # Migrazione: se la tabella esiste senza old_date_planned, lo aggiungo
    try:
        c.execute("ALTER TABLE odoo_writes ADD COLUMN old_date_planned TEXT")
    except sqlite3.OperationalError:
        pass  # colonna già esistente
    # Migrazione: added_po_line_ids per tracciare POL extra aggiunte
    # (pattern operatore MATCH_PARZIALE_OK + spese accessorie). CSV di IDs
    # pertinenti al rollback, NULL se non rilevante.
    try:
        c.execute("ALTER TABLE odoo_writes ADD COLUMN added_po_line_ids TEXT")
    except sqlite3.OperationalError:
        pass
    # Migrazione: extra_po_lines_json per consume-POL multi (Autostrade
    # consuma 2 POL per fattura: furgoni + uso_promiscuo). JSON list di dict
    # {po_line_id, old_price_unit, old_name, old_date_planned, cls}. NULL
    # se non applicabile.
    try:
        c.execute("ALTER TABLE odoo_writes ADD COLUMN extra_po_lines_json TEXT")
    except sqlite3.OperationalError:
        pass
    # Migrazione: PDF split per fatture Autostrade (R4 split Furgoni/Promiscuo)
    # pdf_path: percorso file PDF caricato dall'utente
    # pdf_split_furgoni: imponibile attribuito a 420160 (Furgoni 100%)
    # pdf_split_promiscuo: imponibile attribuito a 420840 (Uso Promiscuo 70%)
    # pdf_split_warnings: JSON list di warnings (apparati non mappati ecc.)
    for col, ddl in (
        ("pdf_path", "TEXT"),
        ("pdf_split_furgoni", "REAL"),
        ("pdf_split_promiscuo", "REAL"),
        ("pdf_split_warnings", "TEXT"),
        # Stato ricezione merci (snapshot della run): 'SI' = tutte le righe
        # merce (product/consu) dell'OdA risolto sono ricevute completamente;
        # 'NO' = almeno una riga merce non ancora (del tutto) ricevuta;
        # NULL = nessun OdA risolto o nessuna riga merce (servizi/no-OdA).
        ("ricezione_merci", "TEXT"),
    ):
        try:
            c.execute(f"ALTER TABLE analyses ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_writes_analysis ON odoo_writes(analysis_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_writes_move ON odoo_writes(move_id)")

    conn.commit()
    conn.close()


def get_db():
    """Ritorna connessione SQLite con row factory dict."""
    conn = sqlite3.connect(app.config['DB_PATH'])
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# ESECUZIONE AGENT IN BACKGROUND
# ============================================================

def run_agent_async(limit=None):
    """Esegue l'agent in thread separato, scrive log nella coda."""
    global is_running

    with _run_lock:
        if is_running:
            log_queue.put(("error", "Un'esecuzione è già in corso"))
            return
        is_running = True

    try:
        log_queue.put(("info", "=== Avvio analisi ==="))

        # Credenziali
        env_file = ROOT / 'config' / 'credentials.env'
        if not env_file.exists():
            log_queue.put(("error", "credentials.env mancante"))
            return
        load_dotenv(env_file)

        url = os.getenv('ODOO_URL')
        db = os.getenv('ODOO_DB')
        user = os.getenv('ODOO_USERNAME')
        pwd = os.getenv('ODOO_PASSWORD')
        company = int(os.getenv('ODOO_COMPANY_ID', '1'))

        log_queue.put(("info", f"Connessione a {url}"))
        client = OdooReadOnlyClient(url, db, user, pwd)
        client.connect()
        log_queue.put(("success", "Connesso a Odoo"))

        log_queue.put(("info", "Recupero fatture 'Da registrare'..."))
        attachments = client.get_fatturapa_attachments(
            only_unregistered=True,
            exclude_self_invoice=True,
            company_id=company,
            limit=limit,
        )
        n_total = len(attachments)
        log_queue.put(("info", f"Trovati {n_total} allegati"))

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

        # Creo run record
        conn = get_db()
        c = conn.cursor()
        started = datetime.now().isoformat()
        c.execute("""INSERT INTO runs (started_at, status, total_analyzed)
                     VALUES (?, 'running', ?)""", (started, n_total))
        run_id = c.lastrowid
        conn.commit()

        analyses = []
        for i, att in enumerate(attachments, 1):
            supplier = att.get('xml_supplier_id')
            sup_name = supplier[1] if isinstance(supplier, list) and len(supplier) > 1 else 'N/D'
            log_queue.put(("progress", {
                "current": i, "total": n_total,
                "supplier": sup_name,
                "name": att.get('name', '')[:60]
            }))

            try:
                a = analyzer.analyze(att)
                analyses.append(a)
            except Exception as e:
                log_queue.put(("warn", f"Errore su {att.get('name')}: {e}"))

        # Post-processing: guardia duplicati per match implicito
        log_queue.put(("info", "Applicazione guardia duplicati..."))
        analyzer.apply_duplicate_guard(analyses)

        # Post-processing: guardia "stretto vince su largo"
        log_queue.put(("info", "Applicazione guardia strict-wins..."))
        analyzer.apply_strict_wins_over_loose(analyses)

        # Post-processing: guardia cumulativa di run su OdA espliciti
        log_queue.put(("info", "Applicazione guardia cumulativa di run..."))
        analyzer.apply_run_cumulative_check(analyses)

        # Salvo tutte le analisi nel DB (dopo guardie). Passo il client per
        # calcolare lo stato ricezione merci (snapshot) dalle po_lines.
        _PRODUCT_TYPE_CACHE.clear()
        for a in analyses:
            _save_analysis(c, run_id, a, client=client)
        conn.commit()

        # Statistiche finali
        from collections import Counter
        counts = Counter(a.classification for a in analyses)
        ended = datetime.now().isoformat()

        c.execute("""
            UPDATE runs SET ended_at=?, status='completed',
            auto_validabili=?, match_implicito=?, match_implicito_ambiguo=?,
            match_parziale=?, match_parziale_ambiguo=?,
            parziali_cumul=?, trasporto_ok=?,
            da_verificare=?, cumul_eccede=?, oda_non_trovato=?,
            no_oda=?, no_oda_con_suggerimenti=?, match_da_suggerimento=?,
            match_da_suggerimento_extra=?,
            mappatura_fornitore=?, mappatura_automezzi=?,
            commesse=?, anomalie=?, total_amount=?
            WHERE id=?
        """, (
            ended,
            counts.get('AUTO_VALIDABILE', 0),
            counts.get('MATCH_IMPLICITO', 0),
            counts.get('MATCH_IMPLICITO_AMBIGUO', 0),
            counts.get('MATCH_PARZIALE_OK', 0),
            counts.get('MATCH_PARZIALE_AMBIGUO', 0),
            counts.get('PARZIALE_CUMULATIVO_OK', 0),
            counts.get('TRASPORTO_OK', 0),
            counts.get('DA_VERIFICARE', 0),
            counts.get('CUMULATIVO_ECCEDE', 0),
            counts.get('ODA_RIFERITO_NON_TROVATO', 0),
            counts.get('NO_ODA_DA_CLASSIFICARE', 0),
            counts.get('NO_ODA_CON_SUGGERIMENTI', 0),
            counts.get('MATCH_DA_SUGGERIMENTO', 0),
            counts.get('MATCH_DA_SUGGERIMENTO_PIU_EXTRA', 0),
            counts.get('MAPPATURA_FORNITORE_FISSO', 0),
            counts.get('MAPPATURA_AUTOMEZZI', 0),
            counts.get('COMMESSA_DETECTED', 0),
            counts.get('ANOMALIA', 0),
            sum(a.invoice_total for a in analyses),
            run_id,
        ))
        conn.commit()
        conn.close()

        log_queue.put(("success", f"Completato: {n_total} fatture analizzate"))
        for cls, n in counts.most_common():
            log_queue.put(("info", f"  {cls}: {n}"))

        log_queue.put(("done", {"run_id": run_id}))

    except Exception as e:
        log_queue.put(("error", f"Errore esecuzione: {e}"))
    finally:
        is_running = False


# Cache di run product_id -> product.type (popolata lazy in _calc_ricezione_merci).
# Evita query ripetute: gli stessi prodotti ricorrono su molte righe/fatture.
_PRODUCT_TYPE_CACHE = {}


def _calc_ricezione_merci(client, a):
    """Stato ricezione merci per la fattura (snapshot della run).

    Ritorna 'SI' se TUTTE le righe MERCE (product.type in product/consu)
    dell'OdA risolto sono ricevute completamente (qty_received >= product_qty);
    'NO' se almeno una riga merce non lo è; None se non c'è OdA risolto o
    l'OdA non ha righe merce (es. soli servizi) → la UI mostrerà un trattino.
    """
    po_lines = getattr(a, 'po_lines', None)
    # Fallback: l'OdA è risolto ma le righe non sono state pre-caricate in
    # questo ramo del classifier → le carico al volo dall'order_line.
    if not po_lines and getattr(a, 'purchase_order', None):
        try:
            order_line = a.purchase_order.get('order_line') or []
            if order_line:
                po_lines = client.get_purchase_order_lines(order_line)
        except Exception:
            po_lines = None
    if not po_lines:
        return None

    # product.type per le righe (bulk sui mancanti in cache)
    prod_ids = [pl['product_id'][0] for pl in po_lines
                if pl.get('product_id') and isinstance(pl['product_id'], list)]
    missing = [pid for pid in set(prod_ids) if pid not in _PRODUCT_TYPE_CACHE]
    if missing:
        try:
            for r in client._call('product.product', 'read', missing, fields=['type']):
                _PRODUCT_TYPE_CACHE[r['id']] = r.get('type', '')
        except Exception:
            return None  # in caso di errore Odoo non blocco il salvataggio

    merce_rows = []
    for pl in po_lines:
        pid = pl['product_id'][0] if (pl.get('product_id') and isinstance(pl['product_id'], list)) else None
        if pid is not None and _PRODUCT_TYPE_CACHE.get(pid) in ('product', 'consu'):
            merce_rows.append(pl)

    if not merce_rows:
        return None  # nessuna merce fisica (servizi) → N/A

    for pl in merce_rows:
        qty = pl.get('product_qty') or 0
        rec = pl.get('qty_received') or 0
        if qty > 0 and rec < qty:
            return 'NO'  # almeno una riga merce non ancora ricevuta del tutto
    return 'SI'


def _save_analysis(cursor, run_id, a, client=None):
    """Salva una singola analisi nel DB."""
    po_name = a.purchase_order.get('name', '') if a.purchase_order else ''
    tipo_doc = getattr(a, 'xml_data', None)
    tipo_doc = tipo_doc.tipo_documento if tipo_doc else None
    ricezione_merci = _calc_ricezione_merci(client, a) if client else None
    cursor.execute("""
        INSERT INTO analyses (
            run_id, attachment_id, attachment_name, supplier_name, supplier_vat,
            invoice_number, invoice_date, invoice_total,
            oda_xml, oda_odoo, classification,
            total_diff, total_diff_percent,
            cumulative_others, cumulative_count,
            commesse, actions, warnings, tipo_documento, ricezione_merci
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id, a.attachment_id, a.attachment_name, a.supplier_name, a.supplier_vat,
        a.invoice_number, a.invoice_date, a.invoice_total,
        ', '.join(a.oda_references_in_xml), po_name, a.classification,
        a.total_diff, a.total_diff_percent,
        a.cumulative_other_invoices, a.cumulative_other_count,
        ', '.join(a.commesse_detected),
        ' | '.join(a.actions_suggested),
        ' | '.join(a.warnings),
        tipo_doc, ricezione_merci,
    ))


# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    """Home con KPI ultima esecuzione."""
    conn = get_db()
    last_run = conn.execute(
        "SELECT * FROM runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    total_runs = conn.execute(
        "SELECT COUNT(*) as n FROM runs WHERE status='completed'"
    ).fetchone()['n']
    conn.close()
    return render_template('index.html',
                           last_run=last_run,
                           total_runs=total_runs,
                           is_running=is_running)


@app.route('/api/run', methods=['POST'])
def api_run():
    """Avvia esecuzione agent in background."""
    if is_running:
        return jsonify({"ok": False, "error": "Esecuzione già in corso"}), 400

    limit = request.json.get('limit') if request.is_json else None
    thread = threading.Thread(target=run_agent_async, args=(limit,), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.route('/api/logs/stream')
def api_logs_stream():
    """Server-Sent Events per log in streaming."""
    def generate():
        while True:
            try:
                level, msg = log_queue.get(timeout=1)
                payload = {"level": level, "msg": msg}
                yield f"data: {json.dumps(payload)}\n\n"
                if level == "done" or level == "error":
                    break
            except queue.Empty:
                # Keep-alive
                yield ": ping\n\n"
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/kpi/<int:run_id>')
def api_kpi(run_id):
    """Ritorna KPI di una specifica run."""
    conn = get_db()
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not run:
        abort(404)
    return jsonify(dict(run))


@app.route('/invoices')
@app.route('/invoices/<int:run_id>')
def invoices(run_id=None):
    """Tabella fatture, filtrabile."""
    conn = get_db()
    if not run_id:
        last = conn.execute(
            "SELECT id FROM runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            run_id = last['id']
    if not run_id:
        conn.close()
        return render_template('invoices.html', invoices=[], run_id=None, filters={}, detail_qs='')

    # Filtri dalla query string
    classification = request.args.get('class', '')
    supplier = request.args.get('supplier', '').strip()
    min_amount = request.args.get('min_amount', '')

    query = "SELECT * FROM analyses WHERE run_id=?"
    params = [run_id]
    if classification:
        # Supporta lista di categorie separate da virgola:
        # /invoices?class=MATCH_IMPLICITO_AMBIGUO,MATCH_PARZIALE_AMBIGUO
        cls_list = [c.strip() for c in classification.split(',') if c.strip()]
        if len(cls_list) == 1:
            query += " AND classification=?"
            params.append(cls_list[0])
        elif len(cls_list) > 1:
            placeholders = ','.join('?' * len(cls_list))
            query += f" AND classification IN ({placeholders})"
            params.extend(cls_list)
    if supplier:
        query += " AND supplier_name LIKE ?"
        params.append(f"%{supplier}%")
    if min_amount:
        try:
            query += " AND invoice_total >= ?"
            params.append(float(min_amount))
        except ValueError:
            pass
    query += " ORDER BY invoice_total DESC"

    analyses = conn.execute(query, params).fetchall()

    # Lista categorie per dropdown
    cats = conn.execute(
        "SELECT DISTINCT classification FROM analyses WHERE run_id=? ORDER BY classification",
        (run_id,)
    ).fetchall()

    conn.close()
    # Querystring filtri da propagare ai link "Dettaglio →" così la
    # navigazione avanti/indietro nella pagina di dettaglio resta coerente
    # con la lista filtrata.
    from urllib.parse import urlencode
    fqs = {k: v for k, v in (('class', classification),
                             ('supplier', supplier),
                             ('min_amount', min_amount)) if v}
    detail_qs = ('?' + urlencode(fqs)) if fqs else ''
    return render_template('invoices.html',
                           invoices=analyses,
                           run_id=run_id,
                           classifications=[r['classification'] for r in cats],
                           detail_qs=detail_qs,
                           filters={
                               'class': classification,
                               'supplier': supplier,
                               'min_amount': min_amount,
                           })


@app.route('/invoice/<int:analysis_id>')
def invoice_detail(analysis_id):
    """Dettaglio singola fattura."""
    conn = get_db()
    a = conn.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    if not a:
        conn.close()
        abort(404)

    # Navigazione avanti/indietro: scorre le fatture della STESSA run
    # rispettando gli stessi filtri (class/supplier/min_amount) e lo stesso
    # ordinamento della lista /invoices, così i tasti ←/→ restano coerenti
    # con la vista filtrata da cui l'utente arriva. Senza filtri scorre
    # tutta la run.
    classification = request.args.get('class', '')
    supplier = request.args.get('supplier', '').strip()
    min_amount = request.args.get('min_amount', '')

    nav_query = "SELECT id FROM analyses WHERE run_id=?"
    nav_params = [a['run_id']]
    if classification:
        cls_list = [c.strip() for c in classification.split(',') if c.strip()]
        if len(cls_list) == 1:
            nav_query += " AND classification=?"
            nav_params.append(cls_list[0])
        elif len(cls_list) > 1:
            placeholders = ','.join('?' * len(cls_list))
            nav_query += f" AND classification IN ({placeholders})"
            nav_params.extend(cls_list)
    if supplier:
        nav_query += " AND supplier_name LIKE ?"
        nav_params.append(f"%{supplier}%")
    if min_amount:
        try:
            nav_query += " AND invoice_total >= ?"
            nav_params.append(float(min_amount))
        except ValueError:
            pass
    # Stesso ORDER BY della lista + tiebreaker id per ordine deterministico
    nav_query += " ORDER BY invoice_total DESC, id ASC"
    nav_ids = [r['id'] for r in conn.execute(nav_query, nav_params).fetchall()]
    conn.close()

    prev_id = next_id = nav_pos = nav_total = None
    if analysis_id in nav_ids:
        idx = nav_ids.index(analysis_id)
        nav_pos = idx + 1
        nav_total = len(nav_ids)
        if idx > 0:
            prev_id = nav_ids[idx - 1]
        if idx < len(nav_ids) - 1:
            next_id = nav_ids[idx + 1]

    # Querystring per preservare i filtri nei link prev/next
    from urllib.parse import urlencode
    nav_qs = {k: v for k, v in (('class', classification),
                                ('supplier', supplier),
                                ('min_amount', min_amount)) if v}
    nav_suffix = ('?' + urlencode(nav_qs)) if nav_qs else ''

    # Flag factoring: il fornitore (SARDA/MBFACTA) usa il writer dedicato
    # create_bozza_factoring (box dedicato), NON il pulsante standard
    # MAPPATURA_FORNITORE_FISSO (che cercherebbe righe libere inesistenti).
    is_factoring = False
    try:
        from config.rules import MAPPATURA_FORNITORI_FISSI
        vat = (a['supplier_vat'] or '').strip()
        me = MAPPATURA_FORNITORI_FISSI.get(vat)
        if not me and vat.startswith('IT'):
            me = MAPPATURA_FORNITORI_FISSI.get(vat[2:])
        is_factoring = bool(me and me.get('factoring'))
    except Exception:
        is_factoring = False
    return render_template('invoice_detail.html', a=a, is_factoring=is_factoring,
                           prev_id=prev_id, next_id=next_id,
                           nav_pos=nav_pos, nav_total=nav_total,
                           nav_suffix=nav_suffix)


@app.route('/history')
def history():
    """Storico esecuzioni."""
    conn = get_db()
    runs = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return render_template('history.html', runs=runs)


@app.route('/settings')
def settings():
    """Configurazione."""
    return render_template('settings.html',
                           tol_percent=TOLLERANZA_PERCENTUALE,
                           tol_absolute=TOLLERANZA_ASSOLUTA,
                           tol_total=TOLLERANZA_TOTALE_FATTURA)


@app.route('/api/run/<int:run_id>/analyses')
def api_run_analyses_list(run_id):
    """Lista analisi di una run, opzionalmente filtrate per classification.

    Usato dal bulk JS Automezzi per recuperare gli id delle analisi della
    categoria.
    """
    classification = request.args.get('classification')
    conn = get_db()
    try:
        if classification:
            rows = conn.execute(
                "SELECT id, supplier_vat, supplier_name, invoice_number, "
                "invoice_total, classification "
                "FROM analyses WHERE run_id=? AND classification=? "
                "ORDER BY id",
                (run_id, classification)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, supplier_vat, supplier_name, invoice_number, "
                "invoice_total, classification "
                "FROM analyses WHERE run_id=? ORDER BY id",
                (run_id,)).fetchall()
        return jsonify({
            'run_id': run_id,
            'classification': classification,
            'analyses': [dict(r) for r in rows],
        })
    finally:
        conn.close()


@app.route('/api/export/<int:run_id>/<string:category>')
def api_export(run_id, category):
    """Esporta CSV delle fatture di una categoria specifica."""
    import csv
    from io import StringIO
    conn = get_db()
    if category == 'all':
        rows = conn.execute("SELECT * FROM analyses WHERE run_id=?",
                           (run_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM analyses WHERE run_id=? AND classification=?",
            (run_id, category)
        ).fetchall()
    conn.close()

    output = StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys(),
                                delimiter=';')
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            "Content-Disposition": f"attachment; filename=export_{category}_{run_id}.csv"
        }
    )


# ============================================================
# ENDPOINT SCRITTURA ODOO (crea bozze / rollback)
# ============================================================

def _load_analysis_for_write(conn, analysis_id: int) -> Optional[Any]:
    """Ricarica un'analisi + parsing XML fresco per passarla all'OdooWriter.
    Usa il client RW perché chi chiama questo helper scrive su Odoo."""
    from core.fatturapa_analyzer import FatturaPAAnalysis
    from core.odoo_rw_client import OdooReadWriteClient
    from core.fatturapa_parser import parse_from_base64
    import base64 as _b64

    row = conn.execute("SELECT * FROM analyses WHERE id=?",
                       (analysis_id,)).fetchone()
    if not row:
        return None, None

    client = OdooReadWriteClient(
        app.config.get('ODOO_URL'), app.config.get('ODOO_DB'),
        app.config.get('ODOO_USERNAME'), app.config.get('ODOO_PASSWORD')
    )
    client.connect()

    # Rileggo l'attachment fresco dall'Odoo per avere XML aggiornato.
    # `create_date` necessario per popolare la data contabile (data ricezione
    # SdI) sulle bozze - vedi OdooWriter._data_contabile.
    atts = client._call('fatturapa.attachment.in', 'search_read',
        [('id', '=', row['attachment_id'])],
        fields=['id', 'name', 'datas', 'xml_supplier_id', 'create_date'])
    if not atts:
        return None, client
    att = atts[0]

    # Ricreo l'analisi minima per il writer
    a = FatturaPAAnalysis(attachment_id=row['attachment_id'])
    a.attachment_name = att.get('name', '')
    a.attachment_create_date = str(att.get('create_date') or '')
    a.xml_data = parse_from_base64(att['datas'])
    try:
        a.raw_xml = _b64.b64decode(att['datas']).decode('utf-8', errors='replace')
    except Exception:
        a.raw_xml = ""
    a.supplier_name = row['supplier_name']
    a.supplier_vat = row['supplier_vat']
    a.invoice_number = row['invoice_number']
    a.invoice_total = row['invoice_total'] or 0
    a.classification = row['classification']
    return a, client


@app.route('/api/odoo_write/create_draft/<int:analysis_id>', methods=['POST'])
def api_odoo_create_draft(analysis_id):
    """
    Crea una bozza in Odoo per una fattura classificata MAPPATURA_FORNITORE_FISSO.
    Rispetta il flag DRY_RUN della config.
    """
    from core.odoo_writer import OdooWriter
    from config.rules import MAPPATURA_FORNITORI_FISSI, ODOO_WRITE_DRY_RUN

    conn = get_db()
    try:
        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not analysis:
            return {'success': False, 'error': 'Analisi non trovata'}, 404

        # Check classificazione
        if analysis.classification != 'MAPPATURA_FORNITORE_FISSO':
            return {'success': False,
                    'error': f"Classe '{analysis.classification}' non ammessa "
                             f"(solo MAPPATURA_FORNITORE_FISSO)"}, 400

        # Check idempotenza: cerco create_draft di successo NON ancora
        # rollbackato o ripristinato via sync. Se esiste, blocco duplicazione.
        # Se invece c'è stato un rollback successivo, quella bozza non esiste
        # più e la creazione è legittima.
        existing = conn.execute("""
            SELECT ow.id, ow.move_id FROM odoo_writes ow
            WHERE ow.analysis_id=? AND ow.action='create_draft'
              AND ow.success=1 AND ow.dry_run=0
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow2
                  WHERE ow2.analysis_id=ow.analysis_id
                    AND ow2.action IN ('rollback', 'sync_restore')
                    AND ow2.id > ow.id AND ow2.success=1
              )
            ORDER BY ow.id DESC LIMIT 1
        """, (analysis_id,)).fetchone()
        if existing:
            return {'success': False,
                    'error': f"Bozza già creata (move_id={existing['move_id']})"}, 409

        # Recupero mappatura e risolvo multi-contratto
        from config.rules import resolve_mapping_entry
        vat = (analysis.xml_data.cedente_partita_iva or '').upper()
        mapping_raw = MAPPATURA_FORNITORI_FISSI.get(vat)
        if not mapping_raw:
            return {'success': False,
                    'error': f"Fornitore P.IVA {vat} non in mappatura"}, 400

        mapping = resolve_mapping_entry(mapping_raw, analysis.xml_data)
        if not mapping:
            return {'success': False,
                    'error': f"Nessun contratto matchato per P.IVA {vat}"}, 400

        if not mapping.get('auto_write_enabled'):
            return {'success': False,
                    'error': f"Auto-write disabilitato per {mapping['nome']}"}, 400

        # Crea bozza: usa multilinea se multi_contratto, line_groups o
        # line_groups_by_month (es. WE4SERVICES P03696); altrimenti standard.
        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        needs_multilinea = (mapping_raw.get('multi_contratto')
                            or mapping_raw.get('line_groups')
                            or mapping_raw.get('line_groups_by_month'))
        if needs_multilinea:
            result = writer.create_bozza_multilinea(analysis, mapping)
        else:
            result = writer.create_bozza_fornitore_fisso(analysis, mapping)

        # Log su DB
        from datetime import datetime
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             old_price_unit, old_name, old_date_planned,
             error_message, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id,
            datetime.now().isoformat(),
            result.action,
            1 if result.success else 0,
            result.move_id,
            result.po_line_id,
            result.old_price_unit,
            result.old_name,
            result.old_date_planned,
            result.error_message,
            1 if result.dry_run else 0,
        ))
        conn.commit()

        return result.to_dict(), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/rollback/<int:analysis_id>', methods=['POST'])
def api_odoo_rollback(analysis_id):
    """Rollback di una bozza creata per un'analisi."""
    from core.odoo_writer import OdooWriter
    from config.rules import ODOO_WRITE_DRY_RUN

    conn = get_db()
    try:
        # Trova la scrittura da rollbackare. Whitelist:
        # - create_draft: writer Trenitalia/Italo (consume 1 POL ledger)
        # - create_draft_from_oda: writer AUTO_VALIDABILE
        # - create_draft_libera: writer NO_ODA libera (storico)
        # - create_draft_autostrade: writer Autostrade consume-POL (2 POL)
        # - create_draft_telepass_canoni: writer Telepass canoni consume-POL multi
        write_row = conn.execute("""
            SELECT * FROM odoo_writes
            WHERE analysis_id=? AND action IN
                ('create_draft','create_draft_from_oda','create_draft_libera',
                 'create_draft_autostrade','create_draft_telepass_canoni',
                 'create_draft_automezzi','create_draft_edenred_uta',
                 'create_draft_enilive')
              AND success=1 AND dry_run=0
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow2
                  WHERE ow2.analysis_id=odoo_writes.analysis_id
                    AND ow2.action IN ('rollback','sync_restore')
                    AND ow2.id > odoo_writes.id AND ow2.success=1
              )
            ORDER BY id DESC LIMIT 1
        """, (analysis_id,)).fetchone()

        if not write_row:
            return {'success': False,
                    'error': 'Nessuna bozza da rollbackare'}, 404

        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not client:
            return {'success': False,
                    'error': 'Impossibile connettersi a Odoo'}, 500

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        # Recupero attachment_id dall'analisi per de-registrare fatturapa
        attachment_id = analysis.attachment_id if analysis else None
        # old_date_planned può non esistere nella riga se l'install è vecchia
        old_date_planned = None
        try:
            old_date_planned = write_row['old_date_planned']
        except (IndexError, KeyError):
            old_date_planned = None

        # POL extra aggiunte all'OdA in fase di create (pattern operatore
        # MATCH_PARZIALE_OK + accessorie). Rimuove anche queste in rollback.
        added_po_line_ids = None
        try:
            csv = write_row['added_po_line_ids']
            if csv:
                added_po_line_ids = [int(x) for x in csv.split(',') if x.strip()]
        except (IndexError, KeyError, TypeError, ValueError):
            added_po_line_ids = None

        # POL secondarie consumate (consume-POL multi: Autostrade ne consuma 2)
        extra_po_lines = None
        try:
            extra_json = write_row['extra_po_lines_json']
            if extra_json:
                extra_po_lines = json.loads(extra_json)
        except (IndexError, KeyError, TypeError, ValueError):
            extra_po_lines = None

        result = writer.rollback_bozza(
            move_id=write_row['move_id'],
            po_line_id=write_row['po_line_id'],
            old_price_unit=write_row['old_price_unit'],
            old_name=write_row['old_name'],
            old_date_planned=old_date_planned,
            attachment_id=attachment_id,
            added_po_line_ids=added_po_line_ids,
            extra_po_lines=extra_po_lines,
        )

        # Log rollback su DB
        from datetime import datetime
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             error_message, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id,
            datetime.now().isoformat(),
            'rollback',
            1 if result.success else 0,
            write_row['move_id'],
            write_row['po_line_id'],
            result.error_message,
            1 if result.dry_run else 0,
        ))
        conn.commit()

        return result.to_dict(), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/status/<int:analysis_id>')
def api_odoo_status(analysis_id):
    """Ritorna lo stato delle scritture per un'analisi."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM odoo_writes
            WHERE analysis_id=?
            ORDER BY id DESC
        """, (analysis_id,)).fetchall()
        return {
            'writes': [dict(r) for r in rows]
        }
    finally:
        conn.close()


@app.route('/api/odoo_write/draft_autostrade/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_autostrade(analysis_id):
    """
    Crea bozza dedicata per fattura Autostrade (IT07516911000).

    Pre-richiede:
    - L'analisi è di un fornitore mappato in MAPPATURA_FORNITORI_FISSI con
      P.IVA IT07516911000 (Autostrade).
    - Il codice_cliente nell'XML è uno dei 6 cc Ecotel mappati.
    - Per cc Ecotel main: opzionalmente PDF già caricato via
      /api/upload_pdf/<id> (sblocca R4 split automatico). Altrimenti R1
      con 2 righe a importo 0.
    """
    from core.odoo_writer import OdooWriter
    from config.rules import (MAPPATURA_FORNITORI_FISSI, ODOO_WRITE_DRY_RUN,
                                resolve_mapping_entry)

    conn = get_db()
    try:
        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not analysis:
            return jsonify({'success': False, 'error': 'Analisi non trovata'}), 404
        if not client:
            return jsonify({'success': False,
                            'error': 'Impossibile connettersi a Odoo'}), 500

        # Risolvo mapping_entry da P.IVA + codice_cliente
        if not analysis.xml_data:
            return jsonify({'success': False,
                            'error': 'Analisi senza xml_data'}), 400
        cedente_vat = (analysis.xml_data.cedente_partita_iva or '').strip()
        if cedente_vat not in MAPPATURA_FORNITORI_FISSI:
            return jsonify({'success': False,
                            'error': f"Fornitore {cedente_vat} non in MAPPATURA"}), 400
        parent = MAPPATURA_FORNITORI_FISSI[cedente_vat]
        if not parent.get('multi_contratto'):
            return jsonify({'success': False,
                            'error': "Fornitore non multi_contratto"}), 400
        resolved = resolve_mapping_entry(parent, analysis.xml_data)
        if not resolved:
            return jsonify({
                'success': False,
                'error': f"Codice cliente {analysis.xml_data.codice_cliente} "
                         f"non mappato per Autostrade",
            }), 400

        # Recupero pdf_split se l'utente ha caricato il PDF
        a_row = conn.execute("SELECT pdf_split_furgoni, pdf_split_promiscuo "
                              "FROM analyses WHERE id=?",
                              (analysis_id,)).fetchone()
        pdf_split = None
        if a_row and a_row['pdf_split_furgoni'] is not None:
            pdf_split = {
                'imponibile_furgoni': a_row['pdf_split_furgoni'],
                'imponibile_promiscuo': a_row['pdf_split_promiscuo'],
            }

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_autostrade(analysis, resolved, pdf_split)

        # Log su odoo_writes (consume-POL: salvo old_* della 1ª POL +
        # extra_po_lines come JSON per la 2ª POL Autostrade)
        added_pol_csv = (','.join(str(x) for x in result.added_po_line_ids)
                         if result.added_po_line_ids else None)
        extra_pol_json = (json.dumps(result.extra_po_lines)
                            if result.extra_po_lines else None)
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             old_price_unit, old_name, old_date_planned,
             error_message, dry_run, added_po_line_ids, extra_po_lines_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, result.po_line_id,
            result.old_price_unit, result.old_name, result.old_date_planned,
            result.error_message, 1 if result.dry_run else 0,
            added_pol_csv, extra_pol_json,
        ))
        conn.commit()
        return jsonify(result.to_dict()), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/draft_automezzi/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_automezzi(analysis_id):
    """Crea bozza per fattura Automezzi (7 fornitori noleggio).

    Pattern consume-POL multi-line: ogni riga XML consuma 1 POL libera
    sull'OdA target (può essere multi-OdA per fatture UnipolRental/Tecnoalt).
    Conto contabile dedotto da (voce, classificazione veicolo dal Parco Auto).
    Tax_id da TAX_AUTOMEZZI[vat].

    Salva audit con extra_po_lines_json (schema esistente).
    """
    from core.odoo_writer import OdooWriter
    from config.rules import (MAPPATURA_AUTOMEZZI, AUTOMEZZI_VATS,
                                ODOO_WRITE_DRY_RUN)

    conn = get_db()
    try:
        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not analysis:
            return jsonify({'success': False, 'error': 'Analisi non trovata'}), 404
        if not client:
            return jsonify({'success': False,
                            'error': 'Impossibile connettersi a Odoo'}), 500
        if not analysis.xml_data:
            return jsonify({'success': False,
                            'error': 'Analisi senza xml_data'}), 400
        cedente_vat = (analysis.xml_data.cedente_partita_iva or '').strip().upper()
        if cedente_vat not in AUTOMEZZI_VATS:
            return jsonify({'success': False,
                            'error': f"P.IVA {cedente_vat} non in MAPPATURA_AUTOMEZZI"}), 400
        mapping = MAPPATURA_AUTOMEZZI[cedente_vat]

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_automezzi(analysis, mapping)

        # Salva audit (con added_po_line_ids per il pattern Leasys aggregato:
        # se il writer ha creato POL ad-hoc per voci extra (bolli/penali), il
        # rollback automatico le deve poter eliminare via _cleanup_extra_pols)
        added_pol_csv = (','.join(str(x) for x in result.added_po_line_ids)
                          if result.added_po_line_ids else None)
        extra_pol_json = (json.dumps(result.extra_po_lines)
                            if result.extra_po_lines else None)
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             old_price_unit, old_name, old_date_planned,
             error_message, dry_run, added_po_line_ids, extra_po_lines_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, result.po_line_id,
            result.old_price_unit, result.old_name, result.old_date_planned,
            result.error_message, 1 if result.dry_run else 0,
            added_pol_csv, extra_pol_json,
        ))
        conn.commit()
        return jsonify(result.to_dict()), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/draft_telepass_canoni/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_telepass_canoni(analysis_id):
    """Crea bozza per fattura canoni Telepass (IT09771701001).

    Pattern consume-POL multi-line con riscrittura totale POL "TEST €1
    generic" su P03722. Ogni riga XML consuma 1 POL libera. Conto
    contabile dedotto dalla voce identificata nella descrizione XML
    (canone/parcheggio/bollo/quota_associativa).

    Salva audit con extra_po_lines_json (schema esistente da Autostrade).
    """
    from core.odoo_writer import OdooWriter
    from config.rules import (MAPPATURA_FORNITORI_FISSI, ODOO_WRITE_DRY_RUN,
                                resolve_mapping_entry)

    conn = get_db()
    try:
        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not analysis:
            return jsonify({'success': False, 'error': 'Analisi non trovata'}), 404
        if not client:
            return jsonify({'success': False,
                            'error': 'Impossibile connettersi a Odoo'}), 500

        if not analysis.xml_data:
            return jsonify({'success': False,
                            'error': 'Analisi senza xml_data'}), 400
        cedente_vat = (analysis.xml_data.cedente_partita_iva or '').strip()
        if cedente_vat != 'IT09771701001':
            return jsonify({'success': False,
                            'error': f"P.IVA {cedente_vat} non è Telepass"}), 400
        parent = MAPPATURA_FORNITORI_FISSI.get(cedente_vat)
        if not parent:
            return jsonify({'success': False,
                            'error': "Telepass non in MAPPATURA_FORNITORI_FISSI"}), 400
        resolved = resolve_mapping_entry(parent, analysis.xml_data)
        if not resolved:
            return jsonify({
                'success': False,
                'error': (f"Codice cliente {analysis.xml_data.codice_cliente} "
                          f"non mappato per Telepass"),
            }), 400

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_telepass_canoni(analysis, resolved)

        # Log su odoo_writes (consume-POL multi-line: salvo old_* della 1ª POL
        # + extra_po_lines_json per le altre)
        extra_pol_json = (json.dumps(result.extra_po_lines)
                            if result.extra_po_lines else None)
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             old_price_unit, old_name, old_date_planned,
             error_message, dry_run, extra_po_lines_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, result.po_line_id,
            result.old_price_unit, result.old_name, result.old_date_planned,
            result.error_message, 1 if result.dry_run else 0,
            extra_pol_json,
        ))
        conn.commit()
        return jsonify(result.to_dict()), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/draft_enilive/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_enilive(analysis_id):
    """Crea bozza per fattura carte carburante Enilive S.p.A.
    (IT11403240960).

    Pattern analogo a Edenred UTA, ma:
      - Dettaglio carta-per-carta sta nel PDF allegato (parsato da
        core.enilive_pdf_parser), non nelle righe XML.
      - POL pre-pianificate quindicinali su P03731 con keyword
        AUTOMEZZI/AUTOVETTURE: match per periodo.
      - FEE SICUREZZA E GEST: POL creata ad-hoc (account 420190, product 12202).

    Mappa carte Enilive in input/carte_enilive.xlsx (auto-refresh su mtime
    via config/carte_enilive_mapping.py).
    """
    from core.odoo_writer import OdooWriter
    from config.rules import MAPPATURA_FORNITORI_FISSI, ODOO_WRITE_DRY_RUN

    conn = get_db()
    try:
        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not analysis:
            return jsonify({'success': False, 'error': 'Analisi non trovata'}), 404
        if not client:
            return jsonify({'success': False,
                            'error': 'Impossibile connettersi a Odoo'}), 500
        if not analysis.xml_data:
            return jsonify({'success': False,
                            'error': 'Analisi senza xml_data'}), 400
        cedente_vat = (analysis.xml_data.cedente_partita_iva or '').strip()
        if cedente_vat != 'IT11403240960':
            return jsonify({'success': False,
                            'error': f"P.IVA {cedente_vat} non è Enilive"}), 400
        mapping_entry = MAPPATURA_FORNITORI_FISSI.get(cedente_vat)
        if not mapping_entry:
            return jsonify({'success': False,
                            'error': "Enilive non in MAPPATURA_FORNITORI_FISSI"}), 400

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_enilive(analysis, mapping_entry)

        # Log su odoo_writes: consume-POL multi-line in extra_po_lines_json +
        # POL SERVIZIO CREATA tracciata in added_po_line_ids per il rollback
        # (lo schema added_po_line_ids serve a _cleanup_extra_pols di unlinkare
        # la POL creata quando si rolla la bozza).
        extra_pol_json = (json.dumps(result.extra_po_lines)
                            if result.extra_po_lines else None)
        added_pol_csv = (','.join(str(x) for x in result.added_po_line_ids)
                          if result.added_po_line_ids else None)
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             old_price_unit, old_name, old_date_planned,
             error_message, dry_run, added_po_line_ids, extra_po_lines_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, result.po_line_id,
            result.old_price_unit, result.old_name, result.old_date_planned,
            result.error_message, 1 if result.dry_run else 0,
            added_pol_csv, extra_pol_json,
        ))
        conn.commit()
        return jsonify(result.to_dict()), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/draft_edenred_uta/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_edenred_uta(analysis_id):
    """Crea bozza per fattura carte carburante Edenred UTA Mobility
    (IT01696270212).

    Pattern: aggregazione ~114 righe XML in max 4 voci semantiche per
    classificazione fiscale (POOL/uso_promiscuo/super_lusso/SERVIZIO).
    Consume-POL multi-line su P03735 con riscrittura nome+prezzo+tax
    delle POL libere pre-pianificate da Acquisti.

    Mappa carte UTA in config/carte_carburante_mapping.py (rigenerata da
    scripts/generate_carte_carburante_mapping.py partendo da
    input/carte_uta.xlsx).
    """
    from core.odoo_writer import OdooWriter
    from config.rules import MAPPATURA_FORNITORI_FISSI, ODOO_WRITE_DRY_RUN

    conn = get_db()
    try:
        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not analysis:
            return jsonify({'success': False, 'error': 'Analisi non trovata'}), 404
        if not client:
            return jsonify({'success': False,
                            'error': 'Impossibile connettersi a Odoo'}), 500
        if not analysis.xml_data:
            return jsonify({'success': False,
                            'error': 'Analisi senza xml_data'}), 400
        cedente_vat = (analysis.xml_data.cedente_partita_iva or '').strip()
        if cedente_vat != 'IT01696270212':
            return jsonify({'success': False,
                            'error': f"P.IVA {cedente_vat} non è Edenred UTA"}), 400
        mapping_entry = MAPPATURA_FORNITORI_FISSI.get(cedente_vat)
        if not mapping_entry:
            return jsonify({'success': False,
                            'error': "Edenred UTA non in MAPPATURA_FORNITORI_FISSI"}), 400

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_edenred_uta(analysis, mapping_entry)

        # Log su odoo_writes (consume-POL multi-line: 1ª POL nei campi singolari
        # + extra_po_lines_json per le altre)
        extra_pol_json = (json.dumps(result.extra_po_lines)
                            if result.extra_po_lines else None)
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             old_price_unit, old_name, old_date_planned,
             error_message, dry_run, extra_po_lines_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, result.po_line_id,
            result.old_price_unit, result.old_name, result.old_date_planned,
            result.error_message, 1 if result.dry_run else 0,
            extra_pol_json,
        ))
        conn.commit()
        return jsonify(result.to_dict()), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/draft_factoring/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_factoring(analysis_id):
    """Crea bozza per fattura di factoring (SARDA FACTORING / MBFACTA).

    Routing per P.IVA con flag 'factoring' in MAPPATURA_FORNITORI_FISSI.
    Il writer create_bozza_factoring CREA POL nuove sull'OdA-ledger (P03522
    per SARDA) — l'OdA non ha righe libere pre-create — raggruppando le righe
    XML per natura IVA: esente N4 (taxes_esente) + eventuale bollo N1 art.15
    (taxes_bollo). Le POL create sono tracciate in added_po_line_ids per il
    rollback (unlink).
    """
    from core.odoo_writer import OdooWriter
    from config.rules import MAPPATURA_FORNITORI_FISSI, ODOO_WRITE_DRY_RUN

    conn = get_db()
    try:
        analysis, client = _load_analysis_for_write(conn, analysis_id)
        if not analysis:
            return jsonify({'success': False, 'error': 'Analisi non trovata'}), 404
        if not client:
            return jsonify({'success': False,
                            'error': 'Impossibile connettersi a Odoo'}), 500
        if not analysis.xml_data:
            return jsonify({'success': False,
                            'error': 'Analisi senza xml_data'}), 400
        cedente_vat = (analysis.xml_data.cedente_partita_iva or '').strip()
        mapping_entry = MAPPATURA_FORNITORI_FISSI.get(cedente_vat)
        if not mapping_entry and cedente_vat.startswith('IT'):
            mapping_entry = MAPPATURA_FORNITORI_FISSI.get(cedente_vat[2:])
        if not mapping_entry or not mapping_entry.get('factoring'):
            return jsonify({'success': False,
                            'error': f"P.IVA {cedente_vat} non è un fornitore "
                                     f"factoring mappato"}), 400

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_factoring(analysis, mapping_entry)

        # Log su odoo_writes: POL create tracciate in added_po_line_ids per
        # il rollback (unlink delle POL nuove).
        added_pol_csv = (','.join(str(x) for x in result.added_po_line_ids)
                          if result.added_po_line_ids else None)
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             old_price_unit, old_name, old_date_planned,
             error_message, dry_run, added_po_line_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, result.po_line_id,
            result.old_price_unit, result.old_name, result.old_date_planned,
            result.error_message, 1 if result.dry_run else 0,
            added_pol_csv,
        ))
        conn.commit()
        return jsonify(result.to_dict()), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/upload_pdf/<int:analysis_id>', methods=['POST'])
def api_upload_pdf(analysis_id):
    """
    Riceve un PDF caricato dall'utente per una fattura specifica (tipicamente
    Autostrade) e calcola lo split Furgoni/Uso Promiscuo basato sulla mappa
    apparati. Salva il PDF in pdf_inbox/ e i dati split nel record analyses.

    Pre-condizione: l'utente entra in /invoice/<analysis_id>, vede una sezione
    "Carica PDF Autostrade per split automatico", upload il file dal portale
    Telepass.
    """
    from core.pdf_parser import parse_pdf_autostrade, calcola_split_furgoni_promiscuo
    from config.apparati_mapping import get_classificazione

    if 'pdf' not in request.files:
        return jsonify({'success': False, 'error': 'File pdf mancante'}), 400
    pdf_file = request.files['pdf']
    if not pdf_file.filename:
        return jsonify({'success': False, 'error': 'Filename vuoto'}), 400
    if not pdf_file.filename.lower().endswith('.pdf'):
        return jsonify({'success': False, 'error': 'Il file deve essere un PDF'}), 400

    conn = get_db()
    try:
        a = conn.execute("SELECT * FROM analyses WHERE id=?",
                         (analysis_id,)).fetchone()
        if not a:
            return jsonify({'success': False, 'error': 'Analisi non trovata'}), 404

        # Salvo PDF in pdf_inbox/<analysis_id>_<numero_fattura>.pdf
        # (uso analysis_id come prefisso per univocità anche se cambia il numero fattura)
        inbox_dir = ROOT / 'pdf_inbox'
        inbox_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize numero fattura per filesystem
        safe_num = re.sub(r'[^A-Za-z0-9._-]', '_',
                          (a['invoice_number'] or 'unknown'))[:80]
        pdf_path = inbox_dir / f"{analysis_id}_{safe_num}.pdf"
        pdf_file.save(str(pdf_path))

        # Parsing
        try:
            pdf_data = parse_pdf_autostrade(str(pdf_path))
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Parsing PDF fallito: {e}',
                'pdf_path': str(pdf_path),
            }), 500

        if not pdf_data.apparati:
            return jsonify({
                'success': False,
                'error': 'Nessun apparato estratto dal PDF (layout non riconosciuto?)',
                'pdf_path': str(pdf_path),
                'parsing_errors': pdf_data.parsing_errors,
            }), 422

        # Imponibile fattura: lo prendiamo dal record analyses (campo invoice_total
        # è IVA inclusa; serve invece l'imponibile XML).
        # Per Autostrade IVA 22% uniforme: imponibile = total / 1.22
        invoice_total = float(a['invoice_total'] or 0)
        imponibile_xml = round(invoice_total / 1.22, 2)

        split = calcola_split_furgoni_promiscuo(
            pdf_data, imponibile_xml, get_classificazione)

        # Salvo dati split sull'analisi
        warnings_json = json.dumps(split.get('warnings', []), ensure_ascii=False)
        conn.execute("""
            UPDATE analyses
            SET pdf_path=?, pdf_split_furgoni=?, pdf_split_promiscuo=?,
                pdf_split_warnings=?
            WHERE id=?
        """, (
            str(pdf_path),
            split['imponibile_furgoni'],
            split['imponibile_promiscuo'],
            warnings_json,
            analysis_id,
        ))
        conn.commit()

        return jsonify({
            'success': True,
            'pdf_path': str(pdf_path),
            'imponibile_xml_stimato': imponibile_xml,
            'imponibile_furgoni': split['imponibile_furgoni'],
            'imponibile_promiscuo': split['imponibile_promiscuo'],
            'apparati_estratti': len(pdf_data.apparati),
            'apparati_furgoni': len(split['apparati_furgoni']),
            'apparati_promiscuo': len(split['apparati_promiscuo']),
            'apparati_non_mappati': len(split['apparati_non_mappati']),
            'totale_iva_inclusa_pdf': split['totale_iva_inclusa_pdf'],
            'warnings': split['warnings'],
        })
    finally:
        conn.close()


@app.route('/api/odoo_write/sync_drafts', methods=['POST'])
def api_odoo_sync_drafts():
    """
    Verifica per ogni bozza creata dall'agent se esiste ancora in Odoo.
    Se è stata cancellata manualmente, ripristina la riga OdA collegata.

    Utile per sincronizzare lo stato dopo che l'operatore ha cancellato
    direttamente bozze da Odoo bypassando il pulsante rollback della webapp.
    """
    from core.odoo_rw_client import OdooReadWriteClient
    from core.odoo_writer import OdooWriter
    from config.rules import ODOO_WRITE_DRY_RUN

    client = OdooReadWriteClient(
        app.config['ODOO_URL'], app.config['ODOO_DB'],
        app.config['ODOO_USERNAME'], app.config['ODOO_PASSWORD']
    )
    client.connect()
    writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)

    conn = get_db()
    try:
        # Trovo tutte le bozze create con successo (non dry_run) e non ancora
        # rollbackate. Per ciascuna verifico che esista ancora in Odoo.
        rows = conn.execute("""
            SELECT ow.id, ow.analysis_id, ow.move_id, ow.po_line_id,
                   ow.old_price_unit, ow.old_name, ow.old_date_planned
            FROM odoo_writes ow
            WHERE ow.action='create_draft' AND ow.success=1 AND ow.dry_run=0
              AND ow.move_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow2
                  WHERE ow2.analysis_id=ow.analysis_id
                    AND ow2.action IN ('rollback', 'sync_restore')
                    AND ow2.id > ow.id AND ow2.success=1
              )
            ORDER BY ow.id DESC
        """).fetchall()

        results = {'checked': len(rows), 'restored': 0, 'ok': 0, 'errors': 0,
                   'details': []}

        for row in rows:
            exists = writer.check_move_exists(row['move_id'])
            if exists:
                results['ok'] += 1
                continue

            # Bozza cancellata -> ripristino la riga OdA
            if not row['po_line_id']:
                continue

            # Recupero attachment_id dall'analisi associata per de-registrarlo
            att_row = conn.execute(
                "SELECT attachment_id FROM analyses WHERE id=?",
                (row['analysis_id'],)
            ).fetchone()
            attachment_id = att_row['attachment_id'] if att_row else None

            # old_date_planned può mancare se la riga del DB è pre-migrazione
            try:
                old_dp = row['old_date_planned']
            except (IndexError, KeyError):
                old_dp = None

            restore_result = writer.restore_po_line(
                row['po_line_id'],
                old_price_unit=row['old_price_unit'],
                old_name=row['old_name'],
                old_date_planned=old_dp,
                attachment_id=attachment_id,
            )

            if restore_result.success:
                results['restored'] += 1
                # Logga nel DB
                conn.execute("""
                    INSERT INTO odoo_writes
                    (analysis_id, timestamp, action, success, move_id,
                     po_line_id, error_message, dry_run)
                    VALUES (?, ?, 'sync_restore', 1, ?, ?, ?, ?)
                """, (
                    row['analysis_id'],
                    datetime.now().isoformat(),
                    row['move_id'],
                    row['po_line_id'],
                    f"Move {row['move_id']} non trovato in Odoo, riga ripristinata",
                    1 if ODOO_WRITE_DRY_RUN else 0,
                ))
                results['details'].append({
                    'analysis_id': row['analysis_id'],
                    'move_id': row['move_id'],
                    'po_line_id': row['po_line_id'],
                    'action': 'restored',
                })
            else:
                results['errors'] += 1
                results['details'].append({
                    'analysis_id': row['analysis_id'],
                    'move_id': row['move_id'],
                    'error': restore_result.error_message,
                })

        conn.commit()
        return results
    finally:
        conn.close()


@app.route('/api/odoo_write/bulk_create_drafts/<int:run_id>', methods=['POST'])
def api_odoo_bulk_create_drafts(run_id):
    """
    Crea bozze in Odoo per tutte le fatture MAPPATURA_FORNITORE_FISSO
    di una run, filtrate per tipo_documento (TD01 o TD04).
    Salta quelle già create.
    """
    from core.odoo_writer import OdooWriter
    from config.rules import MAPPATURA_FORNITORI_FISSI, ODOO_WRITE_DRY_RUN

    tipo_doc = request.json.get('tipo_documento', 'TD01') if request.is_json else 'TD01'
    if tipo_doc not in ('TD01', 'TD04'):
        return jsonify({'success': False, 'error': f'tipo_documento non valido: {tipo_doc}'}), 400

    # Il pulsante "TD01" copre anche le fatture differite TD24 (stessa logica di registrazione).
    tipo_doc_in = ('TD01', 'TD24') if tipo_doc == 'TD01' else (tipo_doc,)
    placeholders = ','.join('?' * len(tipo_doc_in))

    conn = get_db()
    try:
        # Trovo tutte le analisi MAPPATURA_FORNITORE_FISSO della run con il tipo richiesto
        analyses_rows = conn.execute(f"""
            SELECT a.id FROM analyses a
            WHERE a.run_id=? AND a.classification='MAPPATURA_FORNITORE_FISSO'
              AND a.tipo_documento IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow
                  WHERE ow.analysis_id=a.id AND ow.action='create_draft'
                    AND ow.success=1 AND ow.dry_run=0
                    AND NOT EXISTS (
                        SELECT 1 FROM odoo_writes ow2
                        WHERE ow2.analysis_id=ow.analysis_id
                          AND ow2.action IN ('rollback', 'sync_restore')
                          AND ow2.id > ow.id AND ow2.success=1
                    )
              )
            ORDER BY a.id
        """, (run_id, *tipo_doc_in)).fetchall()

        results = {
            'tipo_documento': tipo_doc,
            'total': len(analyses_rows),
            'created': 0,
            'errors': 0,
            'skipped': 0,
            'details': [],
        }

        if not analyses_rows:
            return jsonify(results)

        for row in analyses_rows:
            analysis_id = row['id']
            # Riusa l'endpoint singolo internamente
            analysis, client = _load_analysis_for_write(conn, analysis_id)
            if not analysis:
                results['skipped'] += 1
                results['details'].append({
                    'analysis_id': analysis_id, 'status': 'skipped',
                    'error': 'Analisi non trovata'})
                continue

            from config.rules import resolve_mapping_entry
            vat = (analysis.xml_data.cedente_partita_iva or '').upper()
            mapping_raw = MAPPATURA_FORNITORI_FISSI.get(vat)
            if not mapping_raw:
                results['skipped'] += 1
                results['details'].append({
                    'analysis_id': analysis_id, 'status': 'skipped',
                    'error': f'Mappatura non trovata per P.IVA {vat}'})
                continue

            mapping = resolve_mapping_entry(mapping_raw, analysis.xml_data)
            if not mapping or not mapping.get('auto_write_enabled'):
                results['skipped'] += 1
                results['details'].append({
                    'analysis_id': analysis_id, 'status': 'skipped',
                    'error': f'Mappatura non attiva per P.IVA {vat}'})
                continue

            try:
                writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
                needs_multilinea = (mapping_raw.get('multi_contratto')
                                    or mapping_raw.get('line_groups')
                                    or mapping_raw.get('line_groups_by_month'))
                if needs_multilinea:
                    result = writer.create_bozza_multilinea(analysis, mapping)
                else:
                    result = writer.create_bozza_fornitore_fisso(analysis, mapping)

                conn.execute("""
                    INSERT INTO odoo_writes
                    (analysis_id, timestamp, action, success, move_id, po_line_id,
                     old_price_unit, old_name, old_date_planned,
                     error_message, dry_run)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    analysis_id,
                    datetime.now().isoformat(),
                    result.action,
                    1 if result.success else 0,
                    result.move_id,
                    result.po_line_id,
                    result.old_price_unit,
                    result.old_name,
                    result.old_date_planned,
                    result.error_message,
                    1 if result.dry_run else 0,
                ))
                conn.commit()

                if result.success:
                    results['created'] += 1
                    results['details'].append({
                        'analysis_id': analysis_id, 'status': 'created',
                        'move_id': result.move_id})
                else:
                    results['errors'] += 1
                    results['details'].append({
                        'analysis_id': analysis_id, 'status': 'error',
                        'error': result.error_message})
            except Exception as e:
                results['errors'] += 1
                results['details'].append({
                    'analysis_id': analysis_id, 'status': 'error',
                    'error': str(e)})

        return jsonify(results)
    finally:
        conn.close()


# ============================================================
# ENDPOINT: AUTO_VALIDABILE (create_bozza_da_oda_matched)
# ============================================================

def _load_analysis_with_match(conn, analysis_id):
    """Ricarica un'analisi con purchase_order popolato (analyzer completo).
    Necessario per il writer create_bozza_da_oda_matched.
    """
    from core.fatturapa_analyzer import FatturaPAAnalyzer
    from core.matcher import InvoiceMatcher
    from core.keyword_rules import classify_line_by_keyword
    from core.odoo_rw_client import OdooReadWriteClient

    row = conn.execute("SELECT * FROM analyses WHERE id=?",
                       (analysis_id,)).fetchone()
    if not row:
        return None, None

    client = OdooReadWriteClient(
        app.config.get('ODOO_URL'), app.config.get('ODOO_DB'),
        app.config.get('ODOO_USERNAME'), app.config.get('ODOO_PASSWORD')
    )
    client.connect()

    atts = client._call('fatturapa.attachment.in', 'search_read',
        [('id', '=', row['attachment_id'])],
        fields=['id', 'name', 'datas', 'xml_supplier_id',
                'invoices_total', 'invoices_date', 'inconsistencies',
                'e_invoice_parsing_error', 'create_date'])
    if not atts:
        return None, client

    matcher = InvoiceMatcher(
        tol_percent=TOLLERANZA_PERCENTUALE, tol_absolute=TOLLERANZA_ASSOLUTA,
        tol_total=TOLLERANZA_TOTALE_FATTURA,
        keyword_classifier=classify_line_by_keyword)
    analyzer = FatturaPAAnalyzer(
        client, matcher, TOLLERANZA_TOTALE_FATTURA,
        implicit_match_enabled=MATCH_IMPLICITO_ATTIVO,
        implicit_match_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_PERCENT,
        implicit_match_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
        implicit_match_loose_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA,
        implicit_match_loose_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
        partial_match_enabled=MATCH_PARZIALE_ATTIVO,
        suggestions_enabled=SUGGERIMENTI_ATTIVI,
        supplier_mapping_enabled=MAPPATURA_FORNITORI_FISSI_ATTIVA,
        supplier_mapping=MAPPATURA_FORNITORI_FISSI)

    a = analyzer.analyze(atts[0])
    return a, client


@app.route('/api/odoo_write/draft_from_oda/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_from_oda(analysis_id):
    """
    Crea bozza per fatture AUTO_VALIDABILE (OdA esplicito + totale combaciante)
    o MATCH_IMPLICITO (OdA dedotto univocamente da fornitore+importo)
    o MATCH_DA_SUGGERIMENTO (OdA dedotto da subset-match univoco).
    Replica il flusso "Crea fattura fornitore da OdA": le move line vengono
    ricostruite dalle PO line, NON dalle righe XML. Conto/IVA dedotti da prodotto.
    """
    AMMESSE = ('AUTO_VALIDABILE', 'MATCH_IMPLICITO',
               'MATCH_DA_SUGGERIMENTO', 'MATCH_DA_SUGGERIMENTO_PIU_EXTRA',
               'MATCH_PARZIALE_OK', 'PARZIALE_CUMULATIVO_OK',
               'DA_VERIFICARE', 'CUMULATIVO_ECCEDE')
    from core.odoo_writer import OdooWriter
    from config.rules import ODOO_WRITE_DRY_RUN

    conn = get_db()
    try:
        # Idempotenza: rifiuto se esiste già un create_draft non rollbackato
        existing = conn.execute("""
            SELECT ow.id, ow.move_id FROM odoo_writes ow
            WHERE ow.analysis_id=? AND ow.action IN ('create_draft', 'create_draft_from_oda', 'create_draft_libera')
              AND ow.success=1 AND ow.dry_run=0
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow2
                  WHERE ow2.analysis_id=ow.analysis_id
                    AND ow2.action IN ('rollback', 'sync_restore')
                    AND ow2.id > ow.id AND ow2.success=1
              )
            ORDER BY ow.id DESC LIMIT 1
        """, (analysis_id,)).fetchone()
        if existing:
            return {'success': False,
                    'error': f"Bozza già creata (move_id={existing['move_id']})"}, 409

        analysis, client = _load_analysis_with_match(conn, analysis_id)
        if not analysis:
            return {'success': False, 'error': 'Analisi non trovata'}, 404

        # Verifico che la classificazione sia ancora ammessa
        # (potrebbe essere cambiata se la run è vecchia)
        if analysis.classification not in AMMESSE:
            return {'success': False,
                    'error': f"Classificazione attuale '{analysis.classification}' non ammessa "
                             f"(consentite: {', '.join(AMMESSE)})"}, 400

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_da_oda_matched(analysis)

        from datetime import datetime
        added_pol_csv = (','.join(str(x) for x in result.added_po_line_ids)
                         if result.added_po_line_ids else None)
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             error_message, dry_run, added_po_line_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, result.po_line_id,
            result.error_message, 1 if result.dry_run else 0,
            added_pol_csv,
        ))
        conn.commit()
        return result.to_dict(), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/draft_libera/<int:analysis_id>', methods=['POST'])
def api_odoo_draft_libera(analysis_id):
    """
    Crea bozza "libera da XML" per fatture DA_VERIFICARE con OdA univoco.
    Ricostruisce le move_line direttamente dalle righe XML (importi reali della
    fattura) senza toccare le PO line. L'OdA viene messo solo come riferimento
    testuale (invoice_origin). La connessione contabile-OdA va riconciliata
    manualmente in Odoo dal contabile.

    Requisiti: classification='DA_VERIFICARE' AND oda_odoo valorizzato.
    """
    AMMESSE = ('DA_VERIFICARE',)
    from core.odoo_writer import OdooWriter
    from config.rules import ODOO_WRITE_DRY_RUN

    conn = get_db()
    try:
        # Idempotenza
        existing = conn.execute("""
            SELECT ow.id, ow.move_id FROM odoo_writes ow
            WHERE ow.analysis_id=? AND ow.action IN
                  ('create_draft', 'create_draft_from_oda', 'create_draft_libera')
              AND ow.success=1 AND ow.dry_run=0
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow2
                  WHERE ow2.analysis_id=ow.analysis_id
                    AND ow2.action IN ('rollback', 'sync_restore')
                    AND ow2.id > ow.id AND ow2.success=1
              )
            ORDER BY ow.id DESC LIMIT 1
        """, (analysis_id,)).fetchone()
        if existing:
            return {'success': False,
                    'error': f"Bozza già creata (move_id={existing['move_id']})"}, 409

        analysis, client = _load_analysis_with_match(conn, analysis_id)
        if not analysis:
            return {'success': False, 'error': 'Analisi non trovata'}, 404

        if analysis.classification not in AMMESSE:
            return {'success': False,
                    'error': f"Classificazione attuale '{analysis.classification}' "
                             f"non ammessa (solo DA_VERIFICARE per draft_libera)"}, 400

        if not analysis.purchase_order:
            return {'success': False,
                    'error': "OdA univoco non determinato: impossibile creare "
                             "bozza libera senza riferimento OdA"}, 400

        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
        result = writer.create_bozza_libera_da_xml(analysis)

        from datetime import datetime
        conn.execute("""
            INSERT INTO odoo_writes
            (analysis_id, timestamp, action, success, move_id, po_line_id,
             error_message, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            analysis_id, datetime.now().isoformat(),
            result.action, 1 if result.success else 0,
            result.move_id, None,
            result.error_message, 1 if result.dry_run else 0,
        ))
        conn.commit()
        return result.to_dict(), (200 if result.success else 500)
    finally:
        conn.close()


@app.route('/api/odoo_write/bulk_drafts_from_oda/<int:run_id>', methods=['POST'])
def api_odoo_bulk_drafts_from_oda(run_id):
    """
    Crea bozze in bulk per AUTO_VALIDABILE / MATCH_IMPLICITO / MATCH_DA_SUGGERIMENTO
    di una run, filtrate per tipo_documento. Salta quelle già create.
    Body: {"tipo_documento": "TD01"|"TD04",
           "classification": "AUTO_VALIDABILE"|"MATCH_IMPLICITO"|"MATCH_DA_SUGGERIMENTO"}
           (default AUTO_VALIDABILE)
    """
    from core.odoo_writer import OdooWriter
    from config.rules import ODOO_WRITE_DRY_RUN

    AMMESSE = ('AUTO_VALIDABILE', 'MATCH_IMPLICITO',
               'MATCH_DA_SUGGERIMENTO', 'MATCH_DA_SUGGERIMENTO_PIU_EXTRA',
               'MATCH_PARZIALE_OK', 'PARZIALE_CUMULATIVO_OK',
               'DA_VERIFICARE', 'CUMULATIVO_ECCEDE')

    body = request.json if request.is_json else {}
    tipo_doc = body.get('tipo_documento', 'TD01')
    classification = body.get('classification', 'AUTO_VALIDABILE')

    if tipo_doc not in ('TD01', 'TD04'):
        return jsonify({'success': False,
                        'error': f'tipo_documento non valido: {tipo_doc}'}), 400
    if classification not in AMMESSE:
        return jsonify({'success': False,
                        'error': f'classification non ammessa: {classification} '
                                 f'(consentite: {", ".join(AMMESSE)})'}), 400

    # Il pulsante "TD01" copre anche le fatture differite TD24 (stessa logica di registrazione).
    tipo_doc_in = ('TD01', 'TD24') if tipo_doc == 'TD01' else (tipo_doc,)
    placeholders = ','.join('?' * len(tipo_doc_in))

    conn = get_db()
    try:
        rows = conn.execute(f"""
            SELECT a.id FROM analyses a
            WHERE a.run_id=? AND a.classification=?
              AND a.tipo_documento IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow
                  WHERE ow.analysis_id=a.id
                    AND ow.action IN ('create_draft', 'create_draft_from_oda', 'create_draft_libera')
                    AND ow.success=1 AND ow.dry_run=0
                    AND NOT EXISTS (
                        SELECT 1 FROM odoo_writes ow2
                        WHERE ow2.analysis_id=ow.analysis_id
                          AND ow2.action IN ('rollback', 'sync_restore')
                          AND ow2.id > ow.id AND ow2.success=1
                    )
              )
            ORDER BY a.id
        """, (run_id, classification, *tipo_doc_in)).fetchall()

        results = {
            'tipo_documento': tipo_doc,
            'classification': classification,
            'total': len(rows), 'created': 0, 'errors': 0, 'skipped': 0,
            'details': [],
        }

        for r in rows:
            analysis_id = r['id']
            analysis, client = _load_analysis_with_match(conn, analysis_id)
            if not analysis:
                results['skipped'] += 1
                results['details'].append({'analysis_id': analysis_id,
                                          'status': 'skipped',
                                          'error': 'Analisi non trovata'})
                continue
            if analysis.classification != classification:
                results['skipped'] += 1
                results['details'].append({'analysis_id': analysis_id,
                                          'status': 'skipped',
                                          'error': f'classification={analysis.classification}'})
                continue
            try:
                writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)
                result = writer.create_bozza_da_oda_matched(analysis)
                from datetime import datetime
                added_pol_csv = (','.join(str(x) for x in result.added_po_line_ids)
                                 if result.added_po_line_ids else None)
                conn.execute("""
                    INSERT INTO odoo_writes
                    (analysis_id, timestamp, action, success, move_id, po_line_id,
                     error_message, dry_run, added_po_line_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    analysis_id, datetime.now().isoformat(),
                    result.action, 1 if result.success else 0,
                    result.move_id, result.po_line_id,
                    result.error_message, 1 if result.dry_run else 0,
                    added_pol_csv,
                ))
                conn.commit()
                if result.success:
                    results['created'] += 1
                    results['details'].append({'analysis_id': analysis_id,
                                              'status': 'created',
                                              'move_id': result.move_id})
                else:
                    results['errors'] += 1
                    results['details'].append({'analysis_id': analysis_id,
                                              'status': 'error',
                                              'error': result.error_message})
            except Exception as e:
                results['errors'] += 1
                results['details'].append({'analysis_id': analysis_id,
                                          'status': 'error',
                                          'error': str(e)})

        return jsonify(results)
    finally:
        conn.close()


# ============================================================
# ENDPOINT: ROLLBACK BULK (qualsiasi categoria)
# ============================================================

@app.route('/api/odoo_write/bulk_rollback/<int:run_id>', methods=['POST'])
def api_odoo_bulk_rollback(run_id):
    """
    Rollback in bulk: cancella tutte le bozze ancora attive di una run,
    eventualmente filtrate per categoria.
    Body opzionale: {"classification": "AUTO_VALIDABILE"}
    """
    from core.odoo_writer import OdooWriter
    from config.rules import ODOO_WRITE_DRY_RUN

    classification = None
    if request.is_json:
        classification = request.json.get('classification')

    conn = get_db()
    try:
        sql = """
            SELECT ow.id as write_id, ow.analysis_id, ow.move_id, ow.po_line_id,
                   ow.old_price_unit, ow.old_name, ow.old_date_planned,
                   ow.added_po_line_ids,
                   a.classification, a.attachment_id
            FROM odoo_writes ow
            JOIN analyses a ON a.id = ow.analysis_id
            WHERE a.run_id = ?
              AND ow.action IN ('create_draft', 'create_draft_from_oda', 'create_draft_libera')
              AND ow.success = 1 AND ow.dry_run = 0
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow2
                  WHERE ow2.analysis_id = ow.analysis_id
                    AND ow2.action IN ('rollback', 'sync_restore')
                    AND ow2.id > ow.id AND ow2.success = 1
              )
        """
        params = [run_id]
        if classification:
            sql += " AND a.classification = ?"
            params.append(classification)
        sql += " ORDER BY ow.id DESC"
        rows = conn.execute(sql, params).fetchall()

        results = {
            'classification': classification,
            'total': len(rows), 'rolled_back': 0, 'errors': 0,
            'details': [],
        }

        # Apro un client RW unico per tutti i rollback
        from core.odoo_rw_client import OdooReadWriteClient
        client = OdooReadWriteClient(
            app.config.get('ODOO_URL'), app.config.get('ODOO_DB'),
            app.config.get('ODOO_USERNAME'), app.config.get('ODOO_PASSWORD')
        )
        client.connect()
        writer = OdooWriter(client, dry_run=ODOO_WRITE_DRY_RUN)

        for r in rows:
            analysis_id = r['analysis_id']
            try:
                old_dp = None
                try:
                    old_dp = r['old_date_planned']
                except (IndexError, KeyError):
                    pass
                added_pol_ids = None
                try:
                    csv = r['added_po_line_ids']
                    if csv:
                        added_pol_ids = [int(x) for x in csv.split(',') if x.strip()]
                except (IndexError, KeyError, TypeError, ValueError):
                    added_pol_ids = None
                result = writer.rollback_bozza(
                    move_id=r['move_id'],
                    po_line_id=r['po_line_id'],
                    old_price_unit=r['old_price_unit'],
                    old_name=r['old_name'],
                    old_date_planned=old_dp,
                    attachment_id=r['attachment_id'],
                    added_po_line_ids=added_pol_ids,
                )
                from datetime import datetime
                conn.execute("""
                    INSERT INTO odoo_writes
                    (analysis_id, timestamp, action, success, move_id, po_line_id,
                     error_message, dry_run)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    analysis_id, datetime.now().isoformat(),
                    'rollback', 1 if result.success else 0,
                    r['move_id'], r['po_line_id'],
                    result.error_message, 1 if result.dry_run else 0,
                ))
                conn.commit()
                if result.success:
                    results['rolled_back'] += 1
                    results['details'].append({'analysis_id': analysis_id,
                                              'move_id': r['move_id'],
                                              'status': 'rolled_back'})
                else:
                    results['errors'] += 1
                    results['details'].append({'analysis_id': analysis_id,
                                              'status': 'error',
                                              'error': result.error_message})
            except Exception as e:
                results['errors'] += 1
                results['details'].append({'analysis_id': analysis_id,
                                          'status': 'error', 'error': str(e)})

        return jsonify(results)
    finally:
        conn.close()


# ============================================================
# ENDPOINT: VERIFICA RICEZIONI MANCANTI + EMAIL MAGAZZINO
# ============================================================

@app.route('/api/odoo_write/pending_receptions/<int:run_id>', methods=['GET'])
def api_odoo_pending_receptions(run_id):
    """
    Verifica per la run quali righe PO non hanno ancora ricezione, aggregato
    per OdA. Filtrato sulla classification passata (default AUTO_VALIDABILE,
    accetta anche MATCH_IMPLICITO).
    Query string: ?classification=AUTO_VALIDABILE|MATCH_IMPLICITO
    """
    from core.odoo_rw_client import OdooReadWriteClient

    AMMESSE = ('AUTO_VALIDABILE', 'MATCH_IMPLICITO', 'MATCH_DA_SUGGERIMENTO', 'MATCH_DA_SUGGERIMENTO_PIU_EXTRA',
               'MATCH_PARZIALE_OK', 'PARZIALE_CUMULATIVO_OK',
               'DA_VERIFICARE', 'CUMULATIVO_ECCEDE')
    classification = request.args.get('classification', 'AUTO_VALIDABILE')
    if classification not in AMMESSE:
        return jsonify({'error': f'classification non ammessa: {classification} '
                                 f'(consentite: {", ".join(AMMESSE)})'}), 400

    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT a.id, a.attachment_id, a.supplier_name, a.supplier_vat,
                   a.invoice_number, a.invoice_date, a.invoice_total,
                   a.oda_odoo
            FROM analyses a
            WHERE a.run_id = ? AND a.classification = ?
              AND NOT EXISTS (
                  SELECT 1 FROM odoo_writes ow
                  WHERE ow.analysis_id=a.id
                    AND ow.action IN ('create_draft', 'create_draft_from_oda', 'create_draft_libera')
                    AND ow.success=1 AND ow.dry_run=0
                    AND NOT EXISTS (
                        SELECT 1 FROM odoo_writes ow2
                        WHERE ow2.analysis_id=ow.analysis_id
                          AND ow2.action IN ('rollback', 'sync_restore')
                          AND ow2.id > ow.id AND ow2.success=1
                    )
              )
            ORDER BY a.id
        """, (run_id, classification)).fetchall()

        client = OdooReadWriteClient(
            app.config.get('ODOO_URL'), app.config.get('ODOO_DB'),
            app.config.get('ODOO_USERNAME'), app.config.get('ODOO_PASSWORD')
        )
        client.connect()

        # Aggrego per OdA
        oda_pending = {}  # oda_name -> dict
        for row in rows:
            oda_name = row['oda_odoo']
            if not oda_name:
                continue
            entry = oda_pending.setdefault(oda_name, {
                'oda_name': oda_name,
                'fornitore': row['supplier_name'],
                'partita_iva': row['supplier_vat'],
                'fatture_in_attesa': [],
                'righe_da_ricevere': [],
                '_po_loaded': False,
            })
            entry['fatture_in_attesa'].append({
                'analysis_id': row['id'],
                'numero': row['invoice_number'],
                'data': row['invoice_date'],
                'totale': row['invoice_total'],
            })
            # Carica info OdA solo una volta per OdA
            if not entry['_po_loaded']:
                pos = client._call('purchase.order', 'search_read',
                    [('name', '=', oda_name)],
                    fields=['id', 'name', 'partner_ref', 'amount_total',
                            'order_line', 'date_order', 'company_id'])
                if pos:
                    po = pos[0]
                    entry['oda_amount_total'] = po.get('amount_total')
                    entry['partner_ref'] = po.get('partner_ref') or ''
                    entry['date_order'] = po.get('date_order') or ''
                    company_id = po.get('company_id')
                    if isinstance(company_id, list):
                        company_id = company_id[0]
                    pl_data = client._call('purchase.order.line', 'read',
                        po.get('order_line', []),
                        fields=['id', 'name', 'product_id', 'product_qty',
                                'qty_received', 'qty_invoiced', 'price_unit',
                                'price_subtotal', 'sequence'])
                    pl_data.sort(key=lambda l: (l.get('sequence') or 0, l['id']))
                    # Tipo prodotto (per filtro merci/servizi)
                    pid_set = []
                    for pl in pl_data:
                        p = pl.get('product_id')
                        if isinstance(p, list) and p:
                            pid_set.append(p[0])
                    pid_set = list(set(pid_set))
                    types_by_id = {}
                    if pid_set:
                        prods = client._call('product.product', 'read',
                            pid_set, fields=['id', 'type'])
                        types_by_id = {p['id']: p.get('type') or 'service' for p in prods}
                    for pl in pl_data:
                        prod = pl.get('product_id')
                        prod_id = prod[0] if isinstance(prod, list) else None
                        prod_type = types_by_id.get(prod_id, 'service')
                        if prod_type not in ('product', 'consu'):
                            continue
                        qty_total = float(pl.get('product_qty') or 0)
                        qty_rec = float(pl.get('qty_received') or 0)
                        qty_da_ricevere = qty_total - qty_rec
                        if qty_da_ricevere <= 0.001:
                            continue
                        entry['righe_da_ricevere'].append({
                            'po_line_id': pl['id'],
                            'descrizione': (pl.get('name') or '')[:120],
                            'qty_totale': qty_total,
                            'qty_ricevuta': qty_rec,
                            'qty_da_ricevere': qty_da_ricevere,
                            'price_subtotal': pl.get('price_subtotal') or 0,
                        })
                entry['_po_loaded'] = True

        # Filtro: solo OdA con almeno 1 riga da ricevere
        result_list = []
        for v in oda_pending.values():
            v.pop('_po_loaded', None)
            if v.get('righe_da_ricevere'):
                result_list.append(v)
        result_list.sort(key=lambda x: x['oda_name'])

        return jsonify({
            'run_id': run_id,
            'classification': classification,
            'totale_oda_in_attesa': len(result_list),
            'totale_fatture_bloccate': sum(len(o['fatture_in_attesa']) for o in result_list),
            'oda_in_attesa': result_list,
        })
    finally:
        conn.close()


@app.route('/api/odoo_write/check_reception/<int:analysis_id>', methods=['GET'])
def api_odoo_check_reception(analysis_id):
    """
    Verifica stato ricezione merce per la singola fattura.
    Carica il PO matchato, per ogni riga di tipo merce (product/consu)
    riporta qty totale / ricevuta / da ricevere.
    """
    conn = get_db()
    try:
        analysis, client = _load_analysis_with_match(conn, analysis_id)
        if not analysis or not analysis.purchase_order:
            return jsonify({'ok': False, 'error': 'Analisi o OdA non trovati'}), 404

        po = analysis.purchase_order
        po_line_ids = po.get('order_line') or []
        if not po_line_ids:
            return jsonify({'ok': True, 'oda_name': po.get('name'),
                            'righe_merce': [], 'tutte_ricevute': True,
                            'note': 'OdA senza righe'})

        po_lines = client._call('purchase.order.line', 'read', po_line_ids,
            fields=['id', 'name', 'product_id', 'product_qty',
                    'qty_received', 'qty_invoiced', 'price_unit',
                    'price_subtotal', 'sequence'])
        po_lines.sort(key=lambda l: (l.get('sequence') or 0, l['id']))

        product_ids = list({pl['product_id'][0] for pl in po_lines
                           if isinstance(pl.get('product_id'), list) and pl['product_id']})
        types_by_id = {}
        if product_ids:
            prods = client._call('product.product', 'read', product_ids,
                fields=['id', 'type'])
            types_by_id = {p['id']: p.get('type') or 'service' for p in prods}

        righe_merce = []
        tutte_ok = True
        for pl in po_lines:
            prod = pl.get('product_id')
            prod_id = prod[0] if isinstance(prod, list) else None
            ptype = types_by_id.get(prod_id, 'service')
            if ptype not in ('product', 'consu'):
                continue
            qty_total = float(pl.get('product_qty') or 0)
            qty_rec = float(pl.get('qty_received') or 0)
            qty_inv = float(pl.get('qty_invoiced') or 0)
            qty_da_rec = qty_total - qty_rec
            qty_disponibile = qty_rec - qty_inv
            ricevuta = qty_da_rec <= 0.001
            if not ricevuta:
                tutte_ok = False
            righe_merce.append({
                'po_line_id': pl['id'],
                'descrizione': (pl.get('name') or '')[:120],
                'product_type': ptype,
                'qty_totale': qty_total,
                'qty_ricevuta': qty_rec,
                'qty_da_ricevere': qty_da_rec,
                'qty_disponibile_per_fattura': qty_disponibile,
                'ricevuta': ricevuta,
                'price_subtotal': pl.get('price_subtotal') or 0,
            })

        return jsonify({
            'ok': True,
            'oda_name': po.get('name'),
            'fornitore': po.get('partner_id')[1] if isinstance(po.get('partner_id'), list) else '',
            'righe_merce': righe_merce,
            'totale_righe_merce': len(righe_merce),
            'tutte_ricevute': tutte_ok,
            'note': 'Tutti i prodotti sono servizi: nessuna ricezione necessaria'
                    if not righe_merce else None,
        })
    finally:
        conn.close()


@app.route('/api/odoo_write/email_magazzino/<int:run_id>', methods=['GET'])
def api_odoo_email_magazzino(run_id):
    """
    Genera testo email pronto per il magazzino con elenco delle ricezioni
    mancanti aggregate per OdA. Propaga il filtro classification.
    Query string: ?classification=AUTO_VALIDABILE|MATCH_IMPLICITO
    Output: text/plain con il corpo dell'email (oggetto + body).
    """
    classification = request.args.get('classification', 'AUTO_VALIDABILE')
    # Riuso l'endpoint sopra per ottenere i dati
    with app.test_client() as tclient:
        resp = tclient.get(
            f'/api/odoo_write/pending_receptions/{run_id}'
            f'?classification={classification}')
        data = resp.get_json() or {}

    if not data.get('oda_in_attesa'):
        return Response("Nessuna ricezione mancante per la run.\n",
                        mimetype='text/plain')

    today = datetime.now().strftime('%d/%m/%Y')
    lines = []
    lines.append(f"Oggetto: Ricezioni merce in attesa per registrazione fatture - {today}")
    lines.append("")
    lines.append("Ciao,")
    lines.append("")
    lines.append("le seguenti fatture non possono essere registrate in contabilità "
                 "finché non viene processata la ricezione delle merci dei seguenti "
                 "ordini di acquisto:")
    lines.append("")
    for entry in data['oda_in_attesa']:
        lines.append(f"OdA {entry['oda_name']} - {entry.get('fornitore', '')}")
        if entry.get('partner_ref'):
            lines.append(f"  Rif. fornitore: {entry['partner_ref']}")
        lines.append(f"  Fatture in attesa ({len(entry['fatture_in_attesa'])}):")
        for ft in entry['fatture_in_attesa']:
            lines.append(f"    - n. {ft['numero']} del {ft['data']} - €{ft['totale']:.2f}")
        lines.append(f"  Righe da ricevere:")
        for r in entry['righe_da_ricevere']:
            lines.append(f"    - {r['descrizione']}")
            lines.append(f"      Quantità totale: {r['qty_totale']:.2f}, "
                        f"ricevuta: {r['qty_ricevuta']:.2f}, "
                        f"da ricevere: {r['qty_da_ricevere']:.2f}")
        lines.append("")
    lines.append("Quando avete processato le ricezioni vi prego di confermare; "
                 "l'agent registrerà automaticamente le fatture al run successivo.")
    lines.append("")
    lines.append("Grazie!")
    lines.append("")
    lines.append(f"-- Generato dall'Odoo Invoice Agent il {today}")

    return Response('\n'.join(lines), mimetype='text/plain')


@app.route('/api/odoo_write/email_magazzino_single/<int:analysis_id>', methods=['GET'])
def api_odoo_email_magazzino_single(analysis_id):
    """
    Genera testo email focalizzato sul singolo OdA della fattura specificata.
    Riusa la logica di check_reception per individuare le righe merce non ricevute.
    Output: text/plain con il corpo dell'email (oggetto + body).
    """
    conn = get_db()
    try:
        analysis, client = _load_analysis_with_match(conn, analysis_id)
        if not analysis or not analysis.purchase_order:
            return Response("Analisi o OdA non trovati.\n",
                            mimetype='text/plain', status=404)

        po = analysis.purchase_order
        po_line_ids = po.get('order_line') or []
        if not po_line_ids:
            return Response("OdA senza righe.\n", mimetype='text/plain')

        po_lines = client._call('purchase.order.line', 'read', po_line_ids,
            fields=['id', 'name', 'product_id', 'product_qty',
                    'qty_received', 'qty_invoiced', 'price_unit',
                    'price_subtotal', 'sequence'])
        po_lines.sort(key=lambda l: (l.get('sequence') or 0, l['id']))

        product_ids = list({pl['product_id'][0] for pl in po_lines
                           if isinstance(pl.get('product_id'), list) and pl['product_id']})
        types_by_id = {}
        if product_ids:
            prods = client._call('product.product', 'read', product_ids,
                fields=['id', 'type'])
            types_by_id = {p['id']: p.get('type') or 'service' for p in prods}

        righe_da_ricevere = []
        for pl in po_lines:
            prod = pl.get('product_id')
            prod_id = prod[0] if isinstance(prod, list) else None
            ptype = types_by_id.get(prod_id, 'service')
            if ptype not in ('product', 'consu'):
                continue
            qty_total = float(pl.get('product_qty') or 0)
            qty_rec = float(pl.get('qty_received') or 0)
            qty_da_rec = qty_total - qty_rec
            if qty_da_rec <= 0.001:
                continue
            righe_da_ricevere.append({
                'descrizione': (pl.get('name') or '')[:120],
                'qty_totale': qty_total,
                'qty_ricevuta': qty_rec,
                'qty_da_ricevere': qty_da_rec,
            })

        if not righe_da_ricevere:
            return Response("Nessuna ricezione mancante per questo OdA.\n",
                            mimetype='text/plain')

        # Recupero dati per il testo
        a_row = conn.execute("""
            SELECT supplier_name, invoice_number, invoice_date, invoice_total,
                   oda_odoo
            FROM analyses WHERE id=?
        """, (analysis_id,)).fetchone()
        oda_name = po.get('name') or a_row['oda_odoo'] or ''
        fornitore = (po.get('partner_id')[1]
                     if isinstance(po.get('partner_id'), list)
                     else a_row['supplier_name'] or '')
        partner_ref = po.get('partner_ref') or ''

        today = datetime.now().strftime('%d/%m/%Y')
        lines = []
        lines.append(f"Oggetto: Ricezione merce in attesa - OdA {oda_name} - {today}")
        lines.append("")
        lines.append("Ciao,")
        lines.append("")
        lines.append(f"la seguente fattura non puo' essere registrata in contabilita' "
                     f"finche' non viene processata la ricezione delle merci sull'OdA "
                     f"{oda_name}:")
        lines.append("")
        lines.append(f"OdA {oda_name} - {fornitore}")
        if partner_ref:
            lines.append(f"  Rif. fornitore: {partner_ref}")
        lines.append(f"  Fattura in attesa:")
        lines.append(f"    - n. {a_row['invoice_number']} del {a_row['invoice_date']} "
                     f"- EUR {(a_row['invoice_total'] or 0):.2f}")
        lines.append(f"  Righe da ricevere:")
        for r in righe_da_ricevere:
            lines.append(f"    - {r['descrizione']}")
            lines.append(f"      Quantita' totale: {r['qty_totale']:.2f}, "
                         f"ricevuta: {r['qty_ricevuta']:.2f}, "
                         f"da ricevere: {r['qty_da_ricevere']:.2f}")
        lines.append("")
        lines.append("Quando avete processato le ricezioni vi prego di confermare; "
                     "l'agent registrera' automaticamente la fattura al run successivo.")
        lines.append("")
        lines.append("Grazie!")
        lines.append("")
        lines.append(f"-- Generato dall'Odoo Invoice Agent il {today}")

        return Response('\n'.join(lines), mimetype='text/plain')
    finally:
        conn.close()


if __name__ == '__main__':
    init_db()
    print("")
    print("=" * 60)
    print("Odoo Invoice Agent - Dashboard")
    print("=" * 60)
    host = os.environ.get('WEBAPP_HOST', '127.0.0.1')
    port = int(os.environ.get('WEBAPP_PORT', 5000))
    print(f"Apri il browser all'indirizzo:")
    print(f"   http://{host}:{port}")
    print("=" * 60)
    print("Premi CTRL+C per arrestare")
    print("")
    app.run(debug=False, host=host, port=port, threaded=True)
