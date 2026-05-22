"""
Rollback emergenza dei 4 move draft scollegati (16/05/2026).

Cosa fa:
- Per ogni MOVE_IDS, legge da dashboard.db (default: webapp/dashboard.db) la riga
  odoo_writes più recente di tipo create_* con success=1.
- Estrae: po_line_id, old_price_unit, old_name, old_date_planned,
  added_po_line_ids, extra_po_lines_json.
- Chiama OdooWriter.rollback_bozza(...).
- NON passa attachment_id (così la de-registrazione viene saltata, evitando il
  bug Odoo "column fatturapa_attachment_in.tipo_documento does not exist").
  Gli attachment sono già registered=False, restano disponibili in e-fatture in
  ingresso una volta che il move è unlinkato.

Uso:
    # SERVER (dashboard.db di prod): default path
    python scripts/rollback_unlinked_moves.py            # DRY-RUN, mostra piano
    python scripts/rollback_unlinked_moves.py --apply    # esegue rollback reale

    # LOCALE con DB copiato dal server:
    python scripts/rollback_unlinked_moves.py --db C:\\path\\to\\dashboard.db
    python scripts/rollback_unlinked_moves.py --db ... --apply
"""
import sys, os, json, argparse, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_rw_client import OdooReadWriteClient
from core.odoo_writer import OdooWriter

# I 4 move scollegati identificati il 16/05
MOVE_IDS = [123961, 123962, 123963, 123964]

DEFAULT_DB = ROOT / 'webapp' / 'dashboard.db'


def load_rollback_params(db_path: Path, move_id: int):
    """Legge la riga odoo_writes più recente per il move e ne estrae i parametri."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT id, action, move_id, po_line_id, old_price_unit, old_name,
               old_date_planned, added_po_line_ids, extra_po_lines_json,
               analysis_id, timestamp, success, dry_run
        FROM odoo_writes
        WHERE move_id = ?
          AND success = 1
          AND (dry_run = 0 OR dry_run IS NULL)
          AND action LIKE 'create%'
        ORDER BY id DESC
        LIMIT 1
    """, (move_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None

    # Parsing JSON arrays/objects (potrebbero essere None, int singolo, lista, dict)
    added_ids = None
    raw_added = row['added_po_line_ids']
    if raw_added not in (None, '', 'null'):
        try:
            parsed = json.loads(raw_added) if isinstance(raw_added, str) else raw_added
        except Exception:
            # Forse è già un valore non-JSON (es. "74100" senza parsing)
            try:
                parsed = int(raw_added)
            except Exception:
                parsed = None
        if isinstance(parsed, int):
            added_ids = [parsed]
        elif isinstance(parsed, list):
            added_ids = [int(x) for x in parsed if x is not None]
        elif parsed is not None:
            added_ids = None

    extras = None
    raw_extras = row['extra_po_lines_json']
    if raw_extras not in (None, '', 'null'):
        try:
            parsed = json.loads(raw_extras) if isinstance(raw_extras, str) else raw_extras
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            extras = parsed
        elif isinstance(parsed, dict):
            extras = [parsed]
        else:
            extras = None

    return {
        'odoo_writes_id': row['id'],
        'analysis_id': row['analysis_id'],
        'action': row['action'],
        'timestamp': row['timestamp'],
        'move_id': row['move_id'],
        'po_line_id': row['po_line_id'],
        'old_price_unit': row['old_price_unit'],
        'old_name': row['old_name'],
        'old_date_planned': row['old_date_planned'],
        'added_po_line_ids': added_ids,
        'extra_po_lines': extras,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(DEFAULT_DB),
                        help='Path al dashboard.db (default: webapp/dashboard.db)')
    parser.add_argument('--apply', action='store_true',
                        help='Esegue il rollback reale (senza, è DRY-RUN solo piano)')
    parser.add_argument('--move', type=int, default=None,
                        help='Lavora su un singolo move_id (default: tutti e 4)')
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERRORE: dashboard.db non trovato in {db_path}")
        sys.exit(1)

    # Selezione move target: singolo (--move) o tutta la lista
    if args.move is not None:
        if args.move not in MOVE_IDS:
            print(f"ATTENZIONE: move {args.move} NON è nella lista standard {MOVE_IDS}")
            print("  Procedo comunque (assumiamo che tu sappia cosa fai).")
        targets = [args.move]
    else:
        targets = list(MOVE_IDS)

    print(f"DB:      {db_path}")
    print(f"MODE:    {'APPLY (reale)' if args.apply else 'DRY-RUN (no scritture)'}")
    print(f"TARGETS: {targets}")
    print()

    # Step 1: carica i parametri per ogni move
    plans = []
    for mid in targets:
        params = load_rollback_params(db_path, mid)
        if not params:
            print(f"  move {mid}: NESSUN RECORD in odoo_writes → salto")
            continue
        plans.append(params)

    if not plans:
        print("Nessun rollback eseguibile. Esco.")
        return

    print("=" * 100)
    print("PIANO DI ROLLBACK")
    print("=" * 100)
    for p in plans:
        print(f"  move {p['move_id']}  action={p['action']}  analysis_id={p['analysis_id']}  "
              f"timestamp={p['timestamp']}")
        print(f"     po_line_id={p['po_line_id']}  old_price_unit={p['old_price_unit']}  "
              f"old_date_planned={p['old_date_planned']}")
        print(f"     old_name={p['old_name']!r}")
        print(f"     added_po_line_ids={p['added_po_line_ids']}")
        print(f"     extra_po_lines={p['extra_po_lines']}")
        print()

    if not args.apply:
        print("DRY-RUN: per eseguire, rilancia con --apply")
        return

    # Step 2: connessione Odoo e rollback reale
    print("=" * 100)
    print("ESECUZIONE ROLLBACK")
    print("=" * 100)
    client = OdooReadWriteClient(
        url=os.environ['ODOO_URL'], db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'], password=os.environ['ODOO_PASSWORD'])
    client.connect()
    writer = OdooWriter(client, dry_run=False)

    for p in plans:
        print(f"\n  ▶ rollback move {p['move_id']} ...")
        try:
            res = writer.rollback_bozza(
                move_id=p['move_id'],
                po_line_id=p['po_line_id'],
                old_price_unit=p['old_price_unit'],
                old_name=p['old_name'],
                old_date_planned=p['old_date_planned'],
                attachment_id=None,   # NON de-registrare (bug cascade Odoo)
                added_po_line_ids=p['added_po_line_ids'],
                extra_po_lines=p['extra_po_lines'],
            )
            if res.success:
                print(f"     OK  → move {p['move_id']} cancellato, POL ripristinata")
            else:
                print(f"     FAIL  → {res.error_message}")
        except Exception as e:
            print(f"     EXC  → {e}")

    print()
    print("FINE rollback.")


if __name__ == '__main__':
    main()
