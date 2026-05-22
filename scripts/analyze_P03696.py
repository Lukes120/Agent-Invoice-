"""
One-shot: analisi OdA P03696 + ricerca fatture passive ferme del fornitore
in fatturapa.attachment.in (registered=False).
Read-only.

Particolarità:
  - numero ordine MAI indicato in fattura -> nessun match esplicito XML->OdA
  - importi VARIABILI -> match implicito su amount non utilizzabile
La diagnostica deve quindi:
  1. capire struttura OdA (ledger-style? righe specifiche con prezzo? aperto?)
  2. recuperare tutte le fatture non-registered del cedente (a prescindere
     dall'importo) e mostrarle per ispezione visiva
  3. fornire indicatori utili (data, totale, riferimento amministrazione,
     descrizioni linee) per capire come legarle a P03696
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'],
    db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'],
    password=os.environ['ODOO_PASSWORD'],
)
client.connect()

ODA_NAME = 'P03696'

print("=" * 90)
print(f"ODA {ODA_NAME}")
print("=" * 90)

po = client.search_purchase_order_by_name(ODA_NAME)
if not po:
    print(f"OdA {ODA_NAME} NON TROVATO")
    sys.exit(1)

for k in ('id', 'name', 'partner_id', 'date_order', 'state',
          'invoice_status', 'amount_untaxed', 'amount_tax',
          'amount_total', 'company_id', 'currency_id'):
    print(f"  {k}: {po.get(k)}")
print(f"  n_lines: {len(po.get('order_line') or [])}")

# Recupero anche origin / note se ci sono (campi extra)
extra = client._call('purchase.order', 'read', [po['id']],
                    fields=['origin', 'notes', 'date_planned',
                            'user_id', 'fiscal_position_id'])
if extra:
    for k, v in extra[0].items():
        if k == 'id':
            continue
        print(f"  {k}: {v}")

lines = client.get_purchase_order_lines(po['order_line'])
print(f"\n  RIGHE OdA ({len(lines)}):")
print("  " + "-" * 86)
total_free = 0
total_used = 0
total_partial = 0
for ln in lines:
    prod = ln.get('product_id')
    prod_name = prod[1] if prod else '-'
    qty = ln.get('product_qty') or 0
    rec = ln.get('qty_received') or 0
    inv = ln.get('qty_invoiced') or 0
    pu = ln.get('price_unit') or 0
    sub = ln.get('price_subtotal') or 0
    # classifica riga
    if inv == 0 and rec == 0:
        flag = 'LIBERA '
        total_free += 1
    elif inv >= qty and qty > 0:
        flag = 'CHIUSA '
        total_used += 1
    else:
        flag = 'PARZIAL'
        total_partial += 1
    print(f"   {flag} id={ln['id']:>7} | qty={qty:>5} rec={rec:>4} inv={inv:>4} "
          f"| price={pu:>10.2f} | subt={sub:>10.2f} | "
          f"tax={ln.get('taxes_id')} | "
          f"name={(ln.get('name') or '')[:55]}")
print(f"\n  -> libere: {total_free} | parziali: {total_partial} | chiuse: {total_used}")

# Estraggo partner per la ricerca attachments
partner = po.get('partner_id')
if not partner:
    print("\nPartner OdA non disponibile. Stop.")
    sys.exit(1)
partner_id = partner[0]
partner_name = partner[1]
partner_info = client.get_partner(partner_id)
print(f"\n  Fornitore: id={partner_id} | {partner_name!r} | VAT={partner_info.get('vat')}")

# Cumulato fatture posted/draft gia' collegate a questo OdA
cum = client.get_invoiced_amount_for_po(po['id'], po_name=ODA_NAME)
print(f"\n  Fatture gia' collegate a {ODA_NAME}:")
print(f"     posted (imponibile): {cum['already_invoiced_posted']:.2f}")
print(f"     draft  (imponibile): {cum['already_invoiced_draft']:.2f}")
print(f"     n fatture: {cum['count_invoices']}")
for inv in cum.get('invoices_info', [])[:15]:
    print(f"     - {inv['state']:>6} | {inv.get('date')} | "
          f"{inv['name']!r} | imp={inv['amount']:.2f}")

print("\n" + "=" * 90)
print(f"ATTACHMENT FATTURAPA NON REGISTRATI per cedente {partner_name!r}")
print("=" * 90)

# Cerco TUTTE le fatture non-registered di questo fornitore
# (non filtro per importo: e' variabile)
atts = client._call(
    'fatturapa.attachment.in', 'search_read',
    [('registered', '=', False),
     ('is_self_invoice', '=', False),
     ('xml_supplier_id', '=', partner_id)],
    fields=['id', 'name', 'att_name', 'xml_supplier_id',
            'invoices_total', 'invoices_date', 'invoices_number',
            'company_id', 'create_date', 'inconsistencies',
            'e_invoice_parsing_error', 'e_invoice_validation_error',
            'in_invoice_ids'],
    order='create_date desc',
    limit=50,
)
print(f"\nTrovati: {len(atts)}")
for a in atts:
    sup = a.get('xml_supplier_id')
    co = a.get('company_id')
    print(f"\n  id={a['id']} | {a.get('att_name')!r}")
    print(f"     n.fattura : {a.get('invoices_number')}")
    print(f"     data ft   : {a.get('invoices_date')}")
    print(f"     totale    : {a.get('invoices_total')}")
    print(f"     cedente   : {sup[1] if sup else '-'}")
    print(f"     company   : {co[1] if co else '-'}")
    print(f"     ricevuta  : {a.get('create_date')}")
    if a.get('in_invoice_ids'):
        print(f"     in_invoice_ids : {a['in_invoice_ids']}")
    if a.get('inconsistencies'):
        print(f"     ⚠ inconsistencies: {a['inconsistencies']}")

# Per ognuna, leggo le righe XML decodificate (linee di dettaglio).
# Usa il modulo fatturapa che dovrebbe esporre attachment -> e_invoice_line_ids
# o simile. Provo a leggere campi extra.
print("\n" + "=" * 90)
print("DETTAGLIO LINEE FATTURA (per ispezione contenuto)")
print("=" * 90)
for a in atts[:10]:
    print(f"\n--- attachment {a['id']} | {a.get('att_name')!r} ---")
    # provo i campi possibili
    try:
        full = client._call('fatturapa.attachment.in', 'read', [a['id']],
                            fields=['e_invoice_line_ids',
                                    'e_invoice_received_date',
                                    'e_invoice_reference',
                                    'invoice_partner_bank_id'])
        if full:
            f0 = full[0]
            print(f"   reference    : {f0.get('e_invoice_reference')}")
            print(f"   linee xml ids: {len(f0.get('e_invoice_line_ids') or [])}")
    except Exception as e:
        print(f"   (read extra non riuscito: {e})")
print("\nDONE.")
