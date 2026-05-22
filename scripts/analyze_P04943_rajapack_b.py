"""Analisi XML fattura RAJAPACK + match riga-per-riga vs OdA P04943."""
import sys, os, base64
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64

client = OdooReadOnlyClient(
    url=os.environ['ODOO_URL'], db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'], password=os.environ['ODOO_PASSWORD'],
)
client.connect()

# Inconsistencies + tutti i campi rilevanti
att = client._call(
    'fatturapa.attachment.in', 'read', [5351870],
    fields=['id', 'att_name', 'inconsistencies', 'e_invoice_parsing_error',
            'e_invoice_validation_error', 'e_invoice_validation_message',
            'datas', 'xml_supplier_id', 'invoices_total', 'invoices_number',
            'invoices_date', 'company_id'],
)[0]

print("=" * 80)
print(f"ATTACHMENT id={att['id']} : {att['att_name']}")
print("=" * 80)
print(f"  cedente: {att['xml_supplier_id']}")
print(f"  n.fattura: {att['invoices_number']} | data: {att['invoices_date']}"
      f" | totale: {att['invoices_total']}")
print(f"  inconsistencies: {att.get('inconsistencies') or '(nessuna)'}")
print(f"  parsing_error: {att.get('e_invoice_parsing_error') or '(nessuno)'}")
print(f"  validation_error: {att.get('e_invoice_validation_error') or '(nessuno)'}")
print(f"  validation_msg: {att.get('e_invoice_validation_message') or '(nessuno)'}")

parsed = parse_from_base64(att['datas'])
print()
print("=" * 80)
print("DATI XML PARSATI")
print("=" * 80)
print(f"  cedente_pi: {parsed.cedente_partita_iva}")
print(f"  cedente: {parsed.cedente_denominazione}")
print(f"  numero: {parsed.numero}  data: {parsed.data}")
print(f"  tipo_documento: {parsed.tipo_documento}")
print(f"  imponibile: {parsed.imponibile_totale}")
print(f"  iva: {parsed.imposta_totale}")
print(f"  totale: {parsed.importo_totale}")
print(f"  oda_riferimenti: {parsed.oda_riferimenti}")
print(f"  oda_grezzi: {parsed.oda_valori_grezzi}")
print(f"  oda_testuali: {parsed.oda_riferimenti_testuali}")
print(f"  commesse: {parsed.commessa_riferimenti}")
print(f"  contratti: {parsed.contratto_riferimenti}")
print(f"  causali: {parsed.causali}")
print(f"  numero righe: {len(parsed.righe)}")
print(f"  righe:")
for i, r in enumerate(parsed.righe, 1):
    print(f"   {i:>2}. desc={(r.descrizione or '')[:60]!r}")
    print(f"       qty={r.quantita} pu={r.prezzo_unitario} "
          f"imp={r.prezzo_totale} aliquota={r.aliquota_iva} um={r.unita_misura}")
    if r.codice_articolo_valore or r.codici_articolo:
        print(f"       codArt: tipo={r.codice_articolo_tipo} "
              f"val={r.codice_articolo_valore} | all={r.codici_articolo}")
    if r.altri_dati_gestionali:
        print(f"       altri_dati: {r.altri_dati_gestionali}")
    if r.riferimenti_oda:
        print(f"       righe_oda: {r.riferimenti_oda}")
    if getattr(r, 'data_inizio_periodo', None) or getattr(r, 'data_fine_periodo', None):
        print(f"       periodo: {r.data_inizio_periodo} - {r.data_fine_periodo}")
    if getattr(r, 'riferimento_amministrazione', None):
        print(f"       rif.amm: {r.riferimento_amministrazione}")

print()
print("=" * 80)
print("CONFRONTO RIGHE XML vs RIGHE OdA P04943")
print("=" * 80)
po = client.search_purchase_order_by_name('P04943')
po_lines = client.get_purchase_order_lines(po['order_line'])
print(f"  OdA imp={po['amount_untaxed']} iva={po['amount_tax']} "
      f"tot={po['amount_total']} | XML imp={parsed.imponibile_totale} "
      f"iva={parsed.imposta_totale} tot={parsed.importo_totale}")
print()
print("  OdA righe:")
for ln in po_lines:
    print(f"    POL {ln['id']}: qty={ln['product_qty']} "
          f"rec={ln['qty_received']} inv={ln['qty_invoiced']} "
          f"price={ln['price_unit']} subt={ln['price_subtotal']} "
          f"tax={ln.get('taxes_id')} | {ln.get('name','')[:60]}")
print("  XML righe:")
for r in parsed.righe:
    print(f"    qty={r.quantita} pu={r.prezzo_unitario} "
          f"imp={r.prezzo_totale} aliquota={r.aliquota_iva}% | "
          f"{(r.descrizione or '')[:60]}")
