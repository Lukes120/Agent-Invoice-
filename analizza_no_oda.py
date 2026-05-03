import os, sys, sqlite3
from pathlib import Path
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Leggo dal DB webapp direttamente
db_path = Path(__file__).parent / 'webapp' / 'dashboard.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Prendo l'ultima run completata
last_run = conn.execute(
    "SELECT id FROM runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
).fetchone()
if not last_run:
    print("Nessuna run trovata")
    sys.exit(1)
run_id = last_run['id']
print(f"Analisi run #{run_id}\n")

# Prendo tutte le NO_ODA
rows = conn.execute("""
    SELECT supplier_name, supplier_vat, invoice_total, warnings
    FROM analyses
    WHERE run_id=? AND classification='NO_ODA_DA_CLASSIFICARE'
""", (run_id,)).fetchall()

total = len(rows)
total_amount = sum(r['invoice_total'] or 0 for r in rows)
print(f"Totale NO_ODA: {total}")
print(f"Importo totale: €{total_amount:,.2f}\n")

# Raggruppo per fornitore
suppliers = defaultdict(lambda: {'count': 0, 'total': 0, 'amounts': []})
for r in rows:
    key = r['supplier_name'] or 'SCONOSCIUTO'
    suppliers[key]['count'] += 1
    suppliers[key]['total'] += r['invoice_total'] or 0
    suppliers[key]['amounts'].append(r['invoice_total'] or 0)

# Ordino per numero di occorrenze
ranked = sorted(suppliers.items(), key=lambda x: (-x[1]['count'], -x[1]['total']))

print("="*80)
print("FORNITORI PIU' FREQUENTI IN NO_ODA")
print("="*80)
print(f"{'#':>3} {'Fornitore':<50} {'Num':>4} {'Totale':>12}")
print("-"*80)
for i, (name, info) in enumerate(ranked[:30], 1):
    print(f"{i:>3} {name[:48]:<50} {info['count']:>4} €{info['total']:>10,.2f}")

# Conto fornitori unici
print(f"\nFornitori unici in NO_ODA: {len(suppliers)}")
print(f"Fornitori con 1 sola fattura: {sum(1 for s in suppliers.values() if s['count']==1)}")
print(f"Fornitori con 2+ fatture: {sum(1 for s in suppliers.values() if s['count']>=2)}")

# Analisi su "ragione" di No_Oda dalla warning
print("\n" + "="*80)
print("RAGIONI NO_ODA (dai warning)")
print("="*80)
reasons = Counter()
for r in rows:
    w = r['warnings'] or ''
    if 'troppe righe' in w.lower():
        reasons['Match parziale saltato (troppe righe)'] += 1
    elif 'scartato' in w.lower():
        reasons['Match parziale scartato (righe extra grosse)'] += 1
    else:
        reasons['Nessun riferimento OdA trovato'] += 1
for reason, count in reasons.most_common():
    print(f"  {reason}: {count}")

# Categorie semantiche (euristica sul nome fornitore)
print("\n" + "="*80)
print("CATEGORIE SEMANTICHE (euristica nome)")
print("="*80)
categories = defaultdict(list)
for r in rows:
    name = (r['supplier_name'] or '').upper()
    if any(k in name for k in ['TRENITALIA', 'ITALO', 'NUOVO TRASPORTO']):
        categories['Treni'].append(r)
    elif any(k in name for k in ['HOTEL', 'B&B', 'MAIO', 'BEACH', 'CASTELLA',
                                   'MADIA', 'MILANINFLAT', 'MM HOTELS',
                                   'GRUPPO LAI', 'AN HOTEL', 'STAY', 'MAESTRALE',
                                   'CAR INN', 'MANILA', 'CALALUNA', 'EUROCENTERWEB',
                                   "L'OASI", 'GHS', 'OSTERIA']):
        categories['Hotel/Ristorazione'].append(r)
    elif any(k in name for k in ['LEASYS', 'UNIPOLRENTAL', 'ATHLON', 'EDENRED',
                                   'EUROPCAR', 'ARVAL', 'LEASE']):
        categories['Leasing/Noleggio auto'].append(r)
    elif any(k in name for k in ['MBFACTA', 'FACTORING', 'FININT', 'SANTANDER',
                                   'BLUE SGR', 'CREDIT', 'BANCA']):
        categories['Factoring/Finanza'].append(r)
    elif any(k in name for k in ['WIND', 'TIM', 'TELECOM', 'VODAFONE', 'ARUBA',
                                   'INFOCERT']):
        categories['Telco/Servizi digitali'].append(r)
    elif any(k in name for k in ['ENI', 'ESSEGI', 'AUTOGRILL', 'PARKING',
                                   'CARBURANTE']):
        categories['Carburanti/Pedaggi'].append(r)
    elif any(k in name for k in ['UMANA', 'AGN ENERGIA', 'WE4SERVICES',
                                   'CLASSPI', 'SECURITY']):
        categories['Servizi vari alle imprese'].append(r)
    else:
        categories['Altro/Da classificare'].append(r)

for cat, items in sorted(categories.items(), key=lambda x: -len(x[1])):
    tot = sum(r['invoice_total'] or 0 for r in items)
    print(f"  {cat}: {len(items)} fatture, totale €{tot:,.2f}")

conn.close()