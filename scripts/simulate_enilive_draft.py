"""
Simulazione DRY-RUN della registrazione fattura Enilive ACQ 29506493 (€4.322,56)
su OdA P03731.

NON SCRIVE NULLA su Odoo. Mostra:
  1) Stato attuale POL su P03731 (libere/usate, keyword POOL/uso_promiscuo/servizio)
  2) Calcolo aggregato per classe (da PDF allegato)
  3) BOZZA account.move che VERREBBE creata (3 move_line)
  4) MODIFICHE che VERREBBERO applicate a P03731 (riassegnazione POL + nuova POL SERVIZIO)
"""
import sys
import os
import base64
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from config.carte_enilive_mapping import (
    CARTE_ENILIVE_BY_NUMERO,
    get_classificazione_carta_enilive,
)
import pdfplumber

ATT_ID = 5351804
PO_NAME = 'P03731'
PARTITA_IVA_ENILIVE = 'IT11403240960'

# Routing fiscale Ecotel (allineato con UTA, ref reference_odoo_ids_ecotel)
# product_id 12202 = "Fornitura di Servizi" (uom_id 68 PZ): è quello già usato
# sulle POL esistenti di P03731, lo riusiamo per la nuova POL SERVIZIO.
ENILIVE_ROUTING = {
    'POOL':          {'account_id': 358,  'account_code': '410300', 'tax_id': 11, 'tax_label': '22% S',     'note': 'Furgoni 100%'},
    'uso_promiscuo': {'account_id': 1125, 'account_code': '410410', 'tax_id': 11, 'tax_label': '22% S',     'note': 'Autovetture 70%'},
    'super_lusso':   {'account_id': 359,  'account_code': '410400', 'tax_id': 73, 'tax_label': '22% 60ind', 'note': 'Amministratore 20%'},
    'SERVIZIO':      {'account_id': 1190, 'account_code': '420190', 'tax_id': 11, 'tax_label': '22% S',     'note': 'Fee servizio',
                      'product_id': 12202, 'product_uom_id': 68},
}

# Keyword nel name della POL pre-pianificata per ogni classe
POL_KEYWORDS = {
    'POOL':          ['automezzi', 'pool', 'furgoni'],
    'uso_promiscuo': ['autovetture', 'uso promiscuo', 'promiscuo'],
    'super_lusso':   ['uso amministratore', 'amministratore', 'super lusso'],
}

RE_TOTALE_CARTA = re.compile(
    r'Totale carta:\s*(\d{17,18})\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)',
    re.IGNORECASE,
)
RE_FEE = re.compile(
    r'^FEE SICUREZZA\s+([\d.,]+)\s+([\d.,]+)\s+22,00\s+([\d.,]+)\s*$',
    re.MULTILINE,
)


def to_float_it(s):
    return float(s.replace('.', '').replace(',', '.'))


def parse_pdf_breakdown(pdf_path):
    """Ritorna (carte: dict numero->totals, fee: dict|None)."""
    carte = {}
    fee = None
    with pdfplumber.open(pdf_path) as pdf:
        full = '\n'.join((p.extract_text() or '') for p in pdf.pages)
    for m in RE_TOTALE_CARTA.finditer(full):
        numero = m.group(1)
        carte[numero] = {
            'totale_lordo': to_float_it(m.group(2)),
            'totale_netto': to_float_it(m.group(3)),
            'imponibile':   to_float_it(m.group(4)),
            'iva':          to_float_it(m.group(5)),
        }
    fee_match = RE_FEE.search(full)
    if fee_match:
        fee = {
            'totale':     to_float_it(fee_match.group(1)),
            'imponibile': to_float_it(fee_match.group(2)),
            'iva':        to_float_it(fee_match.group(3)),
        }
    return carte, full, fee


def aggregate_by_classe(carte):
    agg = defaultdict(lambda: {'imponibile': 0.0, 'iva': 0.0, 'totale': 0.0, 'carte': []})
    for numero, c in carte.items():
        classif = get_classificazione_carta_enilive(numero)
        if classif is None:
            classif = '_NON_IN_MAPPA'
        b = agg[classif]
        b['imponibile'] += c['imponibile']
        b['iva']        += c['iva']
        b['totale']     += c['totale_lordo']
        b['carte'].append(numero)
    return dict(agg)


def find_pol_for_classe(pols, classe):
    """Trova POL libere (qi=0,qr=0,qty>=1) col name contenente le keyword di classe."""
    keywords = POL_KEYWORDS.get(classe, [])
    matched = []
    for pol in pols:
        name = (pol.get('name') or '').lower()
        is_libera = (pol['qty_invoiced'] == 0 and pol['qty_received'] == 0 and pol['product_qty'] >= 1)
        kw_match = next((k for k in keywords if k in name), None)
        if kw_match and is_libera:
            matched.append((pol, kw_match))
    return matched


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    # 1) Recupero PO P03731 + POL
    pos = client._call(
        'purchase.order', 'search_read',
        [['name', '=', PO_NAME]],
        ['id', 'name', 'state', 'date_order', 'amount_total', 'company_id', 'partner_id'],
    )
    if not pos:
        print(f'⚠ PO {PO_NAME} non trovato')
        return
    po = pos[0]
    print('=' * 78)
    print(f'OdA {po["name"]}  id={po["id"]}  state={po["state"]}  total=€{po["amount_total"]:,.2f}')
    print(f'  partner: {po["partner_id"]}')
    print(f'  company: {po["company_id"]}')
    print('=' * 78)

    pols = client._call(
        'purchase.order.line', 'search_read',
        [['order_id', '=', po['id']]],
        ['id', 'name', 'product_qty', 'qty_invoiced', 'qty_received',
         'price_unit', 'price_subtotal', 'taxes_id', 'product_id', 'date_planned'],
        order='id asc',
    )

    libere = [l for l in pols
              if l['qty_invoiced'] == 0 and l['qty_received'] == 0 and l['product_qty'] >= 1]
    usate = [l for l in pols if l['qty_invoiced'] > 0 or l['qty_received'] > 0]

    print(f'\nPOL totali: {len(pols)}  |  libere: {len(libere)}  |  usate: {len(usate)}')
    if libere:
        print(f'\nPOL LIBERE su {PO_NAME}:')
        for l in libere:
            tax = l['taxes_id'][0] if l['taxes_id'] else None
            print(f'  id={l["id"]:>7}  qty={l["product_qty"]:>4}  €{l["price_unit"]:>8.2f}  '
                  f'tax={tax}  name={l["name"][:60]!r}')

    # 2) Recupero fattura pending + PDF
    print('\n' + '=' * 78)
    print(f'FATTURA PENDING (attachment id={ATT_ID})')
    print('=' * 78)
    att = client._call(
        'fatturapa.attachment.in', 'read',
        [ATT_ID],
        ['id', 'name', 'datas', 'create_date'],
    )[0]

    raw = base64.b64decode(att['datas'])
    root = ET.fromstring(raw)
    def t(tag):
        for e in root.iter():
            if e.tag.split('}', -1)[-1] == tag:
                return (e.text or '').strip()
        return ''
    numero_doc = t('Numero')
    data_doc = t('Data')
    importo_totale = float(t('ImportoTotaleDocumento') or 0)
    print(f'  nr.doc: {numero_doc}  data: {data_doc}  importo: €{importo_totale:,.2f}')
    print(f'  create_date attachment (= data contabile): {att["create_date"]}')

    pdf_path = ROOT / 'input' / 'enilive_005245074729506493.pdf'
    carte, full_text, fee = parse_pdf_breakdown(pdf_path)
    agg = aggregate_by_classe(carte)
    print(f'\n  Carte trovate nel PDF: {len(carte)}')
    print(f'  FEE SICUREZZA: {fee!r}')

    print('\n  Aggregato per classe:')
    for cl in sorted(agg.keys()):
        b = agg[cl]
        print(f'    {cl:<20} {b["imponibile"]:>9,.2f}€ imp + {b["iva"]:>7,.2f}€ IVA  '
              f'= {b["totale"]:>9,.2f}€  ({len(b["carte"])} carte)')

    # 3) Mostro BOZZA account.move (simulazione)
    print('\n' + '=' * 78)
    print('SIMULAZIONE: bozza account.move che verrebbe creata')
    print('=' * 78)
    journal_id = 19  # FA-Ecotel
    print(f'  move_type:    in_invoice (TD01)')
    print(f'  partner_id:   1908  (Enilive S.p.A. — Ecotel)')
    print(f'  company_id:   1     (Ecotel Italia)')
    print(f'  journal_id:   {journal_id}    (FA-Ecotel)')
    print(f'  ref:          {numero_doc}')
    print(f'  invoice_date: {data_doc}')
    print(f'  date (contabile): {att["create_date"][:10]}   <- create_date attachment SdI')
    print(f'  l10n_it_vat_settlement_date: fine mese data fattura = 2026-04-30')
    print()
    print(f'  invoice_line_ids:')
    print(f'  {"":>4} {"CLASSE":<16} {"ACCOUNT":<10} {"TAX":<6} {"QTY":>5} {"PRICE":>12} {"SUBT":>12}  PURCHASE_LINE')
    print('  ' + '-' * 100)

    classes_in_invoice = []  # in ordine: POOL, uso_promiscuo, SERVIZIO
    for cl in ('POOL', 'uso_promiscuo', 'super_lusso'):
        if cl in agg and agg[cl]['imponibile'] > 0:
            classes_in_invoice.append(cl)
    if fee:
        classes_in_invoice.append('SERVIZIO')

    for idx, cl in enumerate(classes_in_invoice, start=1):
        routing = ENILIVE_ROUTING[cl]
        if cl == 'SERVIZIO':
            price = fee['imponibile']
            subt = price
        else:
            price = agg[cl]['imponibile']
            subt = price

        # Decide quale POL agganciare
        if cl == 'SERVIZIO':
            pol_label = '<<NUOVA POL DA CREARE>>'
        else:
            matched = find_pol_for_classe(pols, cl)
            if matched:
                pol_id = matched[0][0]['id']
                pol_label = f'POL id={pol_id} (libera, keyword="{matched[0][1]}")'
            else:
                pol_label = '⚠ NESSUNA POL libera per questa classe'

        print(f'  #{idx}  {cl:<16} {routing["account_code"]:<10} '
              f'{routing["tax_id"]:<6} {1.0:>5,.0f} {price:>12,.2f} {subt:>12,.2f}  {pol_label}')

    print('  ' + '-' * 100)
    tot_imp = sum(agg[cl]['imponibile'] for cl in agg if cl in ENILIVE_ROUTING)
    if fee:
        tot_imp += fee['imponibile']
    tot_iva = sum(agg[cl]['iva'] for cl in agg if cl in ENILIVE_ROUTING)
    if fee:
        tot_iva += fee['iva']
    print(f'  {"":<16}                              SOMMA IMP. = {tot_imp:>9,.2f}  +IVA {tot_iva:,.2f} = €{tot_imp+tot_iva:,.2f}')
    print(f'  {"":<16}                              XML totale   = €{importo_totale:,.2f}')
    print(f'  {"":<16}                              DELTA        = €{importo_totale - (tot_imp+tot_iva):+,.2f}')

    # 4) Mostro MODIFICHE a P03731
    print('\n' + '=' * 78)
    print(f'SIMULAZIONE: modifiche a {PO_NAME}')
    print('=' * 78)

    print('\n  A) RIASSEGNAZIONE POL libere già presenti:')
    for cl in ('POOL', 'uso_promiscuo'):
        if cl not in agg:
            continue
        b = agg[cl]
        matched = find_pol_for_classe(pols, cl)
        if matched:
            pol, kw = matched[0]
            new_name = f'Costo carburante ft.n.{numero_doc} - {cl}'
            print(f'    POL id={pol["id"]} (was: {pol["name"][:60]!r})')
            print(f'      -> name      = {new_name!r}')
            print(f'      -> price_unit= {pol["price_unit"]:.2f} -> {b["imponibile"]:.2f}')
            print(f'      -> qty       = {pol["product_qty"]} -> 1')
            print(f'      -> tax       = {pol["taxes_id"]} -> [{ENILIVE_ROUTING[cl]["tax_id"]}]')
        else:
            print(f'    ⚠ Classe {cl} — NESSUNA POL libera trovata (servono nuove POL da Acquisti)')

    print('\n  B) NUOVA POL DA CREARE (SERVIZIO):')
    if fee:
        srv = ENILIVE_ROUTING['SERVIZIO']
        new_pol = {
            'order_id': po['id'],
            'name': f'Fee Sicurezza e Gestione ft.n.{numero_doc}',
            'product_id': srv['product_id'],          # 12202 "Fornitura di Servizi"
            'product_uom': srv['product_uom_id'],     # 68 PZ
            'product_qty': 1,
            'price_unit': fee['imponibile'],
            'taxes_id': [(6, 0, [srv['tax_id']])],
            'date_planned': data_doc,
        }
        for k, v in new_pol.items():
            print(f'      {k}: {v!r}')
        print(f'      -> conto contabile su move_line: {srv["account_code"]} (id {srv["account_id"]})')

    print('\n' + '=' * 78)
    print('FINE SIMULAZIONE — nessuna scrittura effettuata')
    print('=' * 78)


if __name__ == '__main__':
    main()
