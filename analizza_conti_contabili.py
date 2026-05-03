import os, sys
from pathlib import Path
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from core.odoo_client import OdooReadOnlyClient

load_dotenv(Path(__file__).parent / 'config' / 'credentials.env')

client = OdooReadOnlyClient(
    os.getenv('ODOO_URL'), os.getenv('ODOO_DB'),
    os.getenv('ODOO_USERNAME'), os.getenv('ODOO_PASSWORD')
)
client.connect()

# I 7 fornitori pilota
fornitori = [
    ('Trenitalia', 'TRENITALIA'),
    ('Italo', 'ITALO'),
    ('Wind Tre', 'WIND TRE'),
    ('Edenred UTA', 'EDENRED UTA'),
    ('Telecom Italia', 'TELECOM ITALIA'),
    ('Leasys Italia', 'LEASYS'),
    ('UnipolRental', 'UNIPOLRENTAL'),
]

DATE_FROM = '2026-01-15'
DATE_TO = '2026-04-19'

for nome, search_key in fornitori:
    print(f"\n{'='*78}")
    print(f"=== {nome} - analisi conti contabili ===")
    print('='*78)

    partners = client._call('res.partner', 'search_read',
        [('name', 'ilike', search_key), ('supplier_rank', '>', 0)],
        fields=['id', 'name', 'vat'], limit=20)

    if not partners:
        print(f"Nessun partner trovato")
        continue

    partner_ids = [p['id'] for p in partners]

    # Cerco fatture del periodo
    moves = client._call('account.move', 'search_read',
        [('partner_id', 'in', partner_ids),
         ('move_type', '=', 'in_invoice'),
         ('state', 'in', ['posted', 'draft']),
         ('invoice_date', '>=', DATE_FROM),
         ('invoice_date', '<=', DATE_TO)],
        fields=['id', 'name', 'invoice_date', 'invoice_origin',
                'amount_untaxed', 'invoice_line_ids', 'ref'],
        order='invoice_date asc', limit=500)

    if not moves:
        print(f"Nessuna fattura nel periodo")
        continue

    print(f"Fatture totali: {len(moves)}")

    # Raccolgo tutti gli invoice_line_ids per lettura massiva
    all_line_ids = []
    for m in moves:
        all_line_ids.extend(m.get('invoice_line_ids', []))

    if not all_line_ids:
        print("Nessuna riga trovata")
        continue

    # Leggo righe con account_id, quantity, price_subtotal
    lines = client._call('account.move.line', 'search_read',
        [('id', 'in', all_line_ids)],
        fields=['id', 'move_id', 'account_id', 'name', 'price_subtotal',
                'display_type'],
        limit=5000)

    # Prendo solo righe di spesa (non pagamenti/tax lines)
    spesa_lines = [l for l in lines
                   if not l.get('display_type') and l.get('account_id')]

    # Raggruppo per account_id
    account_usage = defaultdict(lambda: {'count': 0, 'amount': 0.0, 'name': ''})
    for l in spesa_lines:
        acc = l.get('account_id')
        if not acc:
            continue
        acc_id = acc[0] if isinstance(acc, list) else acc
        acc_name = acc[1] if isinstance(acc, list) else '?'
        account_usage[acc_id]['count'] += 1
        account_usage[acc_id]['amount'] += l.get('price_subtotal', 0) or 0
        account_usage[acc_id]['name'] = acc_name

    print(f"\nConti contabili utilizzati ({len(account_usage)} distinti):")
    print(f"  {'Conto':<60} {'Righe':>6} {'Importo':>12}")
    print("  " + "-"*80)
    # Ordino per importo decrescente
    sorted_accounts = sorted(account_usage.items(),
                              key=lambda x: -x[1]['amount'])
    for acc_id, info in sorted_accounts:
        print(f"  {info['name'][:58]:<60} {info['count']:>6} €{info['amount']:>10,.2f}")

    # Se c'è un conto dominante (>80%), lo segnalo
    total_amount = sum(info['amount'] for info in account_usage.values())
    if total_amount > 0:
        top = sorted_accounts[0]
        top_pct = (top[1]['amount'] / total_amount) * 100
        print(f"\nConto dominante: '{top[1]['name']}' = {top_pct:.1f}% del totale")
        if top_pct > 80:
            print("-> CONTO UNICO ricorrente (regola automatica possibile)")
        elif len(account_usage) <= 3:
            print("-> POCHI CONTI ricorrenti (2-3 regole)")
        else:
            print("-> MOLTI CONTI diversi (pattern complesso)")