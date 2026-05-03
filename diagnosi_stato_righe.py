import os, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

# Leggo TUTTE le righe di P03524 con tutti i campi rilevanti
po = client.search_purchase_order_by_name('P03524')
lines = client._call('purchase.order.line', 'search_read',
    [('order_id', '=', po['id'])],
    fields=['id', 'name', 'price_unit', 'product_qty',
            'qty_invoiced', 'qty_received', 'qty_received_manual',
            'qty_to_invoice', 'qty_to_receive', 'to_invoice',
            'invoice_lines', 'product_id'])

# Raggruppo per stato
libere = [l for l in lines if (l.get('price_unit') or 0) == 0]
con_move = [l for l in lines if l.get('invoice_lines')]

print(f"Totale righe: {len(lines)}")
print(f"Righe libere (price=0): {len(libere)}")
print(f"Righe con move collegate: {len(con_move)}\n")

# Per ogni riga con move, leggo lo stato del move collegato
print("=" * 100)
print("DETTAGLIO RIGHE OCCUPATE DA MOVE")
print("=" * 100)

move_ids_to_check = set()
for l in con_move:
    for mid in (l.get('invoice_lines') or []):
        move_ids_to_check.add(mid)

# Ottengo i move.line dai loro id, e da lì prendo il move parent
if move_ids_to_check:
    move_lines = client._call('account.move.line', 'search_read',
        [('id', 'in', list(move_ids_to_check))],
        fields=['id', 'move_id', 'quantity', 'price_unit'])
    move_parent_ids = list(set(
        ml['move_id'][0] if isinstance(ml['move_id'], list) else ml['move_id']
        for ml in move_lines if ml.get('move_id')
    ))
    moves = client._call('account.move', 'search_read',
        [('id', 'in', move_parent_ids)],
        fields=['id', 'name', 'state', 'move_type', 'ref', 'amount_total'])
    move_by_id = {m['id']: m for m in moves}
    moveline_to_move = {ml['id']: (ml['move_id'][0] if isinstance(ml['move_id'], list) else ml['move_id']) for ml in move_lines}
    moveline_by_id = {ml['id']: ml for ml in move_lines}
else:
    move_by_id = {}
    moveline_to_move = {}
    moveline_by_id = {}

# Separo righe con move DRAFT vs POSTED
draft_lines = []
posted_lines = []
for l in con_move:
    invl = (l.get('invoice_lines') or [])
    states = []
    for ml_id in invl:
        mv_id = moveline_to_move.get(ml_id)
        if mv_id and mv_id in move_by_id:
            states.append(move_by_id[mv_id]['state'])
    if 'draft' in states:
        draft_lines.append(l)
    elif 'posted' in states:
        posted_lines.append(l)

print(f"\nRighe con move in DRAFT: {len(draft_lines)}")
print(f"Righe con move in POSTED: {len(posted_lines)}\n")

# Mostro 1 riga draft e 1 riga posted a confronto
if draft_lines:
    print("-" * 100)
    print(">>> ESEMPIO riga con move DRAFT (agent?)")
    print("-" * 100)
    l = draft_lines[0]
    for k, v in sorted(l.items()):
        print(f"  {k}: {v}")
    for ml_id in (l.get('invoice_lines') or []):
        ml = moveline_by_id.get(ml_id, {})
        mv_id = moveline_to_move.get(ml_id)
        mv = move_by_id.get(mv_id, {})
        print(f"  -> move_line id={ml_id} qty={ml.get('quantity')} price={ml.get('price_unit')}")
        print(f"     move id={mv.get('id')} state={mv.get('state')} type={mv.get('move_type')} "
              f"ref={mv.get('ref')} tot={mv.get('amount_total')}")

if posted_lines:
    print("\n" + "-" * 100)
    print(">>> ESEMPIO riga con move POSTED (operatore)")
    print("-" * 100)
    # Preferisco una riga con prezzo POSITIVO (fattura, non NC) per confronto pulito
    pos_line = next((l for l in posted_lines if (l.get('price_unit') or 0) > 0), posted_lines[0])
    for k, v in sorted(pos_line.items()):
        print(f"  {k}: {v}")
    for ml_id in (pos_line.get('invoice_lines') or []):
        ml = moveline_by_id.get(ml_id, {})
        mv_id = moveline_to_move.get(ml_id)
        mv = move_by_id.get(mv_id, {})
        print(f"  -> move_line id={ml_id} qty={ml.get('quantity')} price={ml.get('price_unit')}")
        print(f"     move id={mv.get('id')} state={mv.get('state')} type={mv.get('move_type')} "
              f"ref={mv.get('ref')} tot={mv.get('amount_total')}")

    # Cerco anche una riga NC posted (prezzo negativo)
    neg_line = next((l for l in posted_lines if (l.get('price_unit') or 0) < 0), None)
    if neg_line:
        print("\n" + "-" * 100)
        print(">>> ESEMPIO riga NC POSTED (prezzo negativo)")
        print("-" * 100)
        for k, v in sorted(neg_line.items()):
            print(f"  {k}: {v}")
        for ml_id in (neg_line.get('invoice_lines') or []):
            ml = moveline_by_id.get(ml_id, {})
            mv_id = moveline_to_move.get(ml_id)
            mv = move_by_id.get(mv_id, {})
            print(f"  -> move_line id={ml_id} qty={ml.get('quantity')} price={ml.get('price_unit')}")
            print(f"     move id={mv.get('id')} state={mv.get('state')} type={mv.get('move_type')} "
                  f"ref={mv.get('ref')} tot={mv.get('amount_total')}")

# Confronto diretto: qty_invoiced fra draft e posted
if draft_lines and posted_lines:
    d = draft_lines[0]
    p = posted_lines[0]
    print("\n" + "=" * 100)
    print("CONFRONTO DIRETTO: draft vs posted")
    print("=" * 100)
    keys = ['qty_invoiced', 'qty_received', 'qty_received_manual',
            'qty_to_invoice', 'qty_to_receive', 'to_invoice']
    for k in keys:
        print(f"  {k}: draft={d.get(k)} | posted={p.get(k)}")