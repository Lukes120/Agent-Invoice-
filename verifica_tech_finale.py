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

# 1. Journal acquisti (campi corretti per Odoo 14)
print("="*78)
print("=== JOURNAL ACQUISTI DISPONIBILI ===")
print("="*78)
journals = client._call('account.journal', 'search_read',
    [('type', '=', 'purchase'), ('company_id', '=', 1)],
    fields=['id', 'name', 'code', 'type'])
for j in journals:
    print(f"  id={j['id']} | code={j['code']} | name={j['name']}")

# 2. Righe libere in P03524 (prezzo=0 e non fatturate)
print("\n" + "="*78)
print("=== RIGHE LIBERE IN P03524 ===")
print("="*78)
po = client.search_purchase_order_by_name('P03524')
if po:
    line_ids = po.get('order_line', [])
    lines = client._call('purchase.order.line', 'search_read',
        [('id', 'in', line_ids)],
        fields=['id', 'name', 'product_id', 'product_qty', 'price_unit',
                'price_subtotal', 'qty_invoiced', 'taxes_id'])
    
    libere = [l for l in lines if l.get('price_unit') == 0 
              and l.get('qty_invoiced', 0) == 0]
    occupate = [l for l in lines if l.get('price_unit') != 0 
                or l.get('qty_invoiced', 0) != 0]
    
    print(f"Totale righe: {len(lines)}")
    print(f"Righe occupate (fatturate): {len(occupate)}")
    print(f"Righe LIBERE (disponibili per agent): {len(libere)}")
    if libere:
        print(f"\nPrime 3 righe libere:")
        for l in libere[:3]:
            prod = l.get('product_id')
            prod_name = prod[1] if isinstance(prod, list) else '?'
            print(f"  id={l['id']} | prodotto: {prod_name} | desc: '{l.get('name','')}'"
                  f" | qty={l.get('product_qty')}")
            # Tasse associate (importante per l'agent)
            print(f"    taxes_id raw: {l.get('taxes_id')}")

# 3. Partner Trenitalia "ufficiale" (decidiamo quale usare)
print("\n" + "="*78)
print("=== PARTNER TRENITALIA DISPONIBILI ===")
print("="*78)
partners = client._call('res.partner', 'search_read',
    [('vat', '=', 'IT05403151003')],
    fields=['id', 'name', 'vat', 'supplier_rank', 'is_company',
            'property_account_payable_id', 'property_supplier_payment_term_id'])
for p in partners:
    print(f"  id={p['id']} | {p['name']}")
    print(f"    rank fornitore: {p['supplier_rank']} | is_company: {p['is_company']}")
    print(f"    conto fornitori: {p.get('property_account_payable_id')}")
    print(f"    termini pagamento: {p.get('property_supplier_payment_term_id')}")

# 4. Conto 420173 (verifica esista)
print("\n" + "="*78)
print("=== CONTO 420173 ===")
print("="*78)
accounts = client._call('account.account', 'search_read',
    [('code', '=', '420173')],
    fields=['id', 'code', 'name', 'user_type_id', 'company_id'])
for a in accounts:
    print(f"  id={a['id']} | {a['code']} - {a['name']}")
    print(f"    tipo: {a.get('user_type_id')} | company: {a.get('company_id')}")

# 5. Taxes_id raw di una riga P03524 per capire IVA 10%
print("\n" + "="*78)
print("=== TAX ID IVA 10% USATA ===")
print("="*78)
# Prendo una riga fatturata qualsiasi di P03524 per vedere taxes_id
if po and lines:
    riga_campione = next((l for l in lines if l.get('price_unit') > 0), None)
    if riga_campione:
        tax_ids = riga_campione.get('taxes_id', [])
        print(f"Riga campione ha taxes_id: {tax_ids}")
        if tax_ids:
            taxes = client._call('account.tax', 'search_read',
                [('id', 'in', tax_ids)],
                fields=['id', 'name', 'amount', 'type_tax_use'])
            for t in taxes:
                print(f"  id={t['id']} | {t['name']} ({t['amount']}%) | {t['type_tax_use']}")