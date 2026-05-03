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

# Coppie (oda_name, importo_fattura, descrizione_caso)
casi = [
    ('P03435', 36.60, 'NETREALITY 741'),
    ('P03501', 6100.00, 'BIANCHI FRANCESCO 168'),
    ('P03986', 834.00, 'Galbato Muscio 6'),
    ('P03850', 5332.13, 'FRANCIONI ELEONORA 5 o 6'),
]

for oda_name, inv_total_with_iva, caso in casi:
    print(f"\n{'='*70}")
    print(f"=== {caso} (fattura €{inv_total_with_iva}) vs OdA {oda_name} ===")
    print('='*70)

    po = client.search_purchase_order_by_name(oda_name)
    if not po:
        print(f"OdA {oda_name} NON TROVATO")
        continue

    print(f"Fornitore: {po['partner_id'][1]}")
    print(f"Stato OdA: {po['state']} | invoice_status: {po['invoice_status']}")
    print(f"Imponibile: €{po['amount_untaxed']:.2f}")
    print(f"Totale (IVA incl): €{po['amount_total']:.2f}")
    print(f"Data: {po.get('date_order', '?')[:10]}")

    # Imponibile fattura stimato (tolgo IVA 22%, approssimativo)
    inv_imp_est = inv_total_with_iva / 1.22
    print(f"\nImponibile fattura stimato (÷1.22): €{inv_imp_est:.2f}")

    lines = client.get_purchase_order_lines(po.get('order_line', []))
    print(f"\nRighe OdA ({len(lines)}):")
    total_lines = 0
    for l in lines:
        prod = l.get('product_id')
        prod_name = prod[1] if isinstance(prod, list) else '?'
        total_lines += l.get('price_subtotal', 0)
        # Marker se matcha l'imponibile fattura
        match_marker = ""
        if abs(l.get('price_subtotal', 0) - inv_imp_est) < 1.0:
            match_marker = " *** MATCH RIGA ***"
        print(f"  qty={l.get('product_qty'):<6} × €{l.get('price_unit'):<8.4f} "
              f"= €{l.get('price_subtotal'):<10.2f} | {prod_name[:50]}{match_marker}")
    print(f"Somma righe: €{total_lines:.2f}")

    # Account.move collegati (fatture già registrate)
    moves = client._call('account.move', 'search_read',
        [('invoice_origin', '=', oda_name),
         ('move_type', '=', 'in_invoice')],
        fields=['id', 'name', 'state', 'amount_untaxed', 'amount_total',
                'invoice_date', 'ref'],
        order='invoice_date desc', limit=10)
    print(f"\nFatture già collegate (account.move): {len(moves)}")
    for m in moves:
        print(f"  [{m['state']}] {m.get('name', '?')} ref={m.get('ref', '?')} "
              f"€{m['amount_untaxed']:.2f} {m.get('invoice_date', '?')}")
    
    already_invoiced = sum(m['amount_untaxed'] for m in moves
                          if m['state'] in ('draft', 'posted'))
    residuo = po['amount_untaxed'] - already_invoiced
    print(f"Totale già fatturato: €{already_invoiced:.2f}")
    print(f"Residuo OdA: €{residuo:.2f}")
    
    # Check match possibili
    print(f"\n--- Match possibili con imponibile fattura €{inv_imp_est:.2f} ---")
    if abs(po['amount_untaxed'] - inv_imp_est) < 1.0:
        print("  * Totale OdA = fattura (match implicito classico)")
    if abs(residuo - inv_imp_est) < 1.0:
        print("  * Residuo OdA = fattura (match cumulativo implicito)")
    for l in lines:
        if abs(l.get('price_subtotal', 0) - inv_imp_est) < 1.0:
            prod = l.get('product_id')
            prod_name = prod[1] if isinstance(prod, list) else '?'
            print(f"  * Singola riga = fattura ({prod_name[:40]})")