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

# I 5 fornitori pilota (uso il nome parziale per ilike)
fornitori = [
    ('Trenitalia', 'TRENITALIA'),
    ('Italo', 'ITALO'),
    ('Wind Tre', 'WIND TRE'),
    ('Edenred UTA', 'EDENRED'),
    ('Telecom Italia', 'TELECOM'),
]

# Periodo: 15/01/2026 -> oggi
DATE_FROM = '2026-01-15'
DATE_TO = '2026-04-19'

for nome, search_key in fornitori:
    print(f"\n{'='*75}")
    print(f"=== {nome} (ricerca '{search_key}') - periodo {DATE_FROM} - {DATE_TO} ===")
    print('='*75)

    # Cerco partner_id del fornitore
    partners = client._call('res.partner', 'search_read',
        [('name', 'ilike', search_key), ('supplier_rank', '>', 0)],
        fields=['id', 'name', 'vat'], limit=20)

    if not partners:
        print(f"Nessun partner trovato per '{search_key}'")
        continue

    print(f"Partner trovati ({len(partners)}):")
    for p in partners:
        print(f"  id={p['id']} | {p['name']} | P.IVA={p.get('vat')}")

    # Prendo tutti gli ID (un fornitore può avere più record se creato duplicato)
    partner_ids = [p['id'] for p in partners]

    # Cerco tutte le fatture (account.move) del periodo
    moves = client._call('account.move', 'search_read',
        [('partner_id', 'in', partner_ids),
         ('move_type', '=', 'in_invoice'),
         ('state', 'in', ['posted', 'draft']),
         ('invoice_date', '>=', DATE_FROM),
         ('invoice_date', '<=', DATE_TO)],
        fields=['id', 'name', 'state', 'invoice_date', 'invoice_origin',
                'amount_untaxed', 'amount_total', 'ref', 'partner_id'],
        order='invoice_date asc', limit=500)

    print(f"\nFatture registrate nel periodo: {len(moves)}")

    if not moves:
        continue

    # Analizzo distribuzione OdA
    oda_usage = Counter()
    no_oda_count = 0
    total_amount = 0
    for m in moves:
        origin = m.get('invoice_origin') or ''
        if origin.strip():
            oda_usage[origin.strip()] += 1
        else:
            no_oda_count += 1
        total_amount += m.get('amount_untaxed', 0) or 0

    print(f"Importo totale: €{total_amount:,.2f}")
    print(f"Fatture con OdA (invoice_origin): {sum(oda_usage.values())}")
    print(f"Fatture senza OdA: {no_oda_count}")

    if oda_usage:
        print(f"\nDistribuzione OdA:")
        for oda, count in oda_usage.most_common(20):
            print(f"  {oda:<30} -> {count} fatture")

    # Dettaglio fatture (ultime 15)
    print(f"\nUltime 15 fatture:")
    print(f"  {'Data':<12} {'Num':<15} {'Origin':<20} {'Importo':>10} Stato")
    for m in moves[-15:]:
        origin = (m.get('invoice_origin') or '')[:19]
        print(f"  {m.get('invoice_date', '?'):<12} "
              f"{(m.get('ref') or '?')[:14]:<15} "
              f"{origin:<20} "
              f"€{m.get('amount_untaxed', 0):>8.2f} "
              f"{m.get('state')}")

    # Analisi: se ci sono molti OdA diversi, è "on-demand" (trasferte tipo Italo/Trenitalia)
    # Se c'è uno/pochi OdA fissi ripetuti, è "ricorrente" (canoni WIND, Telecom)
    unique_odas = len(oda_usage)
    total_with_oda = sum(oda_usage.values())
    if total_with_oda > 0:
        print(f"\n--- Pattern ---")
        print(f"OdA distinti utilizzati: {unique_odas}")
        print(f"Fatture per OdA (media): {total_with_oda/unique_odas:.1f}")
        if unique_odas == 1:
            print(f"-> OdA UNICO RICORRENTE ({list(oda_usage.keys())[0]})")
        elif unique_odas <= 3:
            print(f"-> POCHI OdA ricorrenti")
        else:
            print(f"-> MOLTI OdA diversi (fatturazione on-demand o diverse sedi)")
    elif no_oda_count > 0:
        print(f"\n--- Pattern ---")
        print(f"-> Nessun OdA usato: probabilmente fatture registrate senza collegamento")