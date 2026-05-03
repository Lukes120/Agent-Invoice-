"""
Script one-shot: backfill della colonna tipo_documento nelle analyses esistenti.
Legge l'XML da Odoo per ogni analisi con tipo_documento NULL e lo aggiorna.

Uso:
    python backfill_tipo_documento.py
"""

import os
import sys
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64

DB_PATH = ROOT / 'webapp' / 'dashboard.db'


def main():
    # Assicuro che la colonna esista
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(analyses)").fetchall()}
    if 'tipo_documento' not in existing_cols:
        conn.execute("ALTER TABLE analyses ADD COLUMN tipo_documento TEXT")
        conn.commit()
        print("Colonna tipo_documento aggiunta alla tabella analyses.")

    # Trovo analisi senza tipo_documento
    rows = conn.execute(
        "SELECT id, attachment_id FROM analyses WHERE tipo_documento IS NULL"
    ).fetchall()

    if not rows:
        print("Nessuna analisi da aggiornare. Tutto gia' backfillato.")
        conn.close()
        return

    print(f"Trovate {len(rows)} analisi senza tipo_documento. Connessione a Odoo...")

    client = OdooReadOnlyClient(
        os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
        os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD'),
    )
    client.connect()
    print("Connesso.")

    # Raccolgo tutti gli attachment_id unici
    att_ids = list({r['attachment_id'] for r in rows if r['attachment_id']})
    print(f"Recupero {len(att_ids)} allegati da Odoo...")

    # Leggo in blocco per efficienza
    atts = client._call(
        'fatturapa.attachment.in', 'search_read',
        [('id', 'in', att_ids)],
        fields=['id', 'datas'],
    )
    att_map = {a['id']: a['datas'] for a in atts if a.get('datas')}
    print(f"Recuperati {len(att_map)} allegati con dati XML.")

    updated = 0
    errors = 0
    for r in rows:
        att_id = r['attachment_id']
        datas = att_map.get(att_id)
        if not datas:
            print(f"  [SKIP] Analisi #{r['id']}: allegato {att_id} senza XML")
            continue
        try:
            xml_data = parse_from_base64(datas)
            tipo_doc = xml_data.tipo_documento
            if tipo_doc:
                conn.execute(
                    "UPDATE analyses SET tipo_documento=? WHERE id=?",
                    (tipo_doc, r['id'])
                )
                updated += 1
            else:
                print(f"  [WARN] Analisi #{r['id']}: tipo_documento vuoto nel XML")
        except Exception as e:
            print(f"  [ERR]  Analisi #{r['id']}: {e}")
            errors += 1

    conn.commit()
    conn.close()
    print(f"\nDone: {updated} aggiornate, {errors} errori, {len(rows) - updated - errors} saltate.")


if __name__ == '__main__':
    main()
