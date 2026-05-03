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

ITALO_VAT = 'IT09247981005'
ITALO_ODA = 'P04279'

# ============================================================
# 1. STATO OdA P04279
# ============================================================
print("="*80)
print("1. STATO OdA P04279")
print("="*80)

po = client.search_purchase_order_by_name(ITALO_ODA)
if not po:
    print(f"ERRORE: OdA {ITALO_ODA} non trovato!")
    sys.exit(1)

keys = ['id', 'name', 'state', 'partner_id', 'amount_untaxed',
        'amount_total', 'invoice_status', 'date_order',
        'company_id', 'currency_id']
for k in keys:
    v = po.get(k)
    print(f"  {k}: {v}")

# ============================================================
# 2. RIGHE LIBERE / OCCUPATE
# ============================================================
print("\n" + "="*80)
print(f"2. RIGHE DI {ITALO_ODA}")
print("="*80)

lines = client._call('purchase.order.line', 'search_read',
    [('order_id', '=', po['id'])],
    fields=['id', 'name', 'product_id', 'product_qty',
            'price_unit', 'qty_invoiced', 'qty_received',
            'taxes_id', 'account_analytic_id', 'date_planned'])

libere = [l for l in lines if (l.get('price_unit') or 0) == 0
          and (l.get('qty_invoiced') or 0) == 0]
occupate = [l for l in lines if (l.get('price_unit') or 0) != 0]

print(f"  Totale righe: {len(lines)}")
print(f"  Righe LIBERE (prezzo=0, qty_inv=0): {len(libere)}")
print(f"  Righe OCCUPATE: {len(occupate)}")

if libere:
    print(f"\n  Prime 3 righe libere:")
    for l in libere[:3]:
        prod = l.get('product_id')
        prod_name = prod[1] if isinstance(prod, list) else '?'
        analytic = l.get('account_analytic_id')
        analytic_name = analytic[1] if isinstance(analytic, list) else '(vuoto)'
        print(f"    id={l['id']} | '{l.get('name','')}' | qty={l.get('product_qty')} | "
              f"analitico='{analytic_name}' | taxes={l.get('taxes_id')} | "
              f"data_cons={l.get('date_planned')}")

if occupate:
    print(f"\n  Prime 3 righe occupate (per referenza):")
    for l in occupate[:3]:
        print(f"    id={l['id']} | '{l.get('name','')}' | prezzo={l.get('price_unit')} | "
              f"qty_inv={l.get('qty_invoiced')} | qty_rec={l.get('qty_received')}")

# ============================================================
# 3. PARTNER ITALO
# ============================================================
print("\n" + "="*80)
print("3. PARTNER ITALO (tutti quelli con P.IVA IT09247981005)")
print("="*80)

partners = client._call('res.partner', 'search_read',
    [('vat', '=', ITALO_VAT)],
    fields=['id', 'name', 'vat', 'supplier_rank', 'is_company',
            'property_account_payable_id',
            'property_supplier_payment_term_id'])
for p in partners:
    print(f"  id={p['id']} | {p['name']}")
    print(f"    rank fornitore: {p.get('supplier_rank')} | is_company: {p.get('is_company')}")
    print(f"    conto fornitori: {p.get('property_account_payable_id')}")
    print(f"    termini pagamento: {p.get('property_supplier_payment_term_id')}")

# ============================================================
# 4. XML DI UNA FATTURA ITALO RECENTE
# ============================================================
print("\n" + "="*80)
print("4. STRUTTURA XML FATTURA ITALO")
print("="*80)

# Cerco l'ultimo fatturapa.attachment.in di Italo
atts = client._call('fatturapa.attachment.in', 'search_read',
    [('xml_supplier_id.vat', '=', ITALO_VAT)],
    fields=['id', 'name', 'datas', 'registered'],
    order='create_date desc', limit=2)

if not atts:
    print("  Nessuna fattura Italo trovata in fatturapa.attachment.in")
else:
    import base64
    import re
    for i, att in enumerate(atts, 1):
        print(f"\n  --- Fattura {i}: {att['name']} (registered={att['registered']}) ---")
        try:
            xml = base64.b64decode(att['datas']).decode('utf-8', errors='replace')

            # Cerco TipoDocumento
            tipodoc = re.search(r'<TipoDocumento>([^<]+)</TipoDocumento>', xml)
            print(f"    TipoDocumento: {tipodoc.group(1) if tipodoc else '?'}")

            # Numero e Data
            numero = re.search(r'<DatiGeneraliDocumento>.*?<Numero>([^<]+)</Numero>', xml, re.DOTALL)
            data = re.search(r'<DatiGeneraliDocumento>.*?<Data>([^<]+)</Data>', xml, re.DOTALL)
            print(f"    Numero: {numero.group(1) if numero else '?'}")
            print(f"    Data: {data.group(1) if data else '?'}")

            # Imponibile totale (prima occorrenza)
            imp = re.search(r'<ImportoTotaleDocumento>([^<]+)</ImportoTotaleDocumento>', xml)
            print(f"    ImportoTotale: {imp.group(1) if imp else '?'}")

            # Righe DettaglioLinee - mostro prime 2
            linee = re.findall(r'<DettaglioLinee>(.*?)</DettaglioLinee>', xml, re.DOTALL)
            print(f"    Numero righe: {len(linee)}")
            for j, lin in enumerate(linee[:2], 1):
                desc = re.search(r'<Descrizione>([^<]+)</Descrizione>', lin)
                qty = re.search(r'<Quantita>([^<]+)</Quantita>', lin)
                prezzo = re.search(r'<PrezzoUnitario>([^<]+)</PrezzoUnitario>', lin)
                prezzo_tot = re.search(r'<PrezzoTotale>([^<]+)</PrezzoTotale>', lin)
                print(f"      Riga {j}:")
                print(f"        Descrizione: '{desc.group(1) if desc else '?'}'")
                print(f"        Qty: {qty.group(1) if qty else '?'}  "
                      f"PrezzoU: {prezzo.group(1) if prezzo else '?'}  "
                      f"PrezzoTot: {prezzo_tot.group(1) if prezzo_tot else '?'}")

                # AltriDatiGestionali di questa riga
                adg = re.findall(
                    r'<AltriDatiGestionali>\s*<TipoDato>([^<]+)</TipoDato>\s*'
                    r'<RiferimentoTesto>([^<]+)</RiferimentoTesto>',
                    lin)
                if adg:
                    print(f"        AltriDatiGestionali:")
                    for td, rt in adg:
                        print(f"          TipoDato='{td.strip()}'  "
                              f"RiferimentoTesto='{rt.strip()}'")
                else:
                    print(f"        AltriDatiGestionali: nessuno")
        except Exception as e:
            print(f"    Errore parsing: {e}")

# ============================================================
# 5. RIASSUNTO
# ============================================================
print("\n" + "="*80)
print("5. RIASSUNTO - Cosa manca per attivare Italo")
print("="*80)

issues = []
if po.get('state') != 'purchase':
    issues.append(f"OdA P04279 state='{po.get('state')}' (dovrebbe essere 'purchase')")
if len(libere) == 0:
    issues.append("Nessuna riga libera in P04279 - serve crearne almeno 1 con prezzo 0")
if not partners:
    issues.append(f"Partner Italo con P.IVA {ITALO_VAT} non trovato")
elif not any(p.get('supplier_rank', 0) > 0 for p in partners):
    issues.append("Il partner Italo ha supplier_rank=0 (mai usato come fornitore)")
if not atts:
    issues.append("Nessuna fattura Italo storica in fatturapa.attachment.in (nulla da testare)")

if issues:
    print("  PROBLEMI RILEVATI:")
    for i in issues:
        print(f"    - {i}")
else:
    print("  TUTTO OK: Italo può essere attivato.")
    print(f"  Partner id da usare: {partners[0]['id'] if partners else '?'}")