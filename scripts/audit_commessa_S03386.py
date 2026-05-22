"""
Audit READ-ONLY commessa S03386 (analytic_account_id = 3813).
Cliente: WE4SERVICES S.C. A R.L. (partner id 585)
Finalita: documento bancario per affidamento sul circolante.

Esegue le 6 verifiche del prompt prompt_verifica_S03386.md e produce un report
markdown in output/audit_commessa_S03386.md.
"""

import sys
import os
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient

ANALYTIC_ID = 3813
SALE_ORDER_ID = 3415
PARTNER_WE4_ID = 585
SO_NAME = 'S03386'

OUTPUT_PATH = ROOT / 'output' / 'audit_commessa_S03386.md'


def fmt_eur(v, decimals=2):
    if v is None:
        return 'N/D'
    s = f"{abs(v):,.{decimals}f}"
    s = s.replace(',', '_').replace('.', ',').replace('_', '.')
    return ('-' if v < 0 else '') + '€ ' + s


def fmt_pct(v, decimals=1):
    if v is None:
        return 'N/D'
    return f"{v:.{decimals}f}%"


def md_table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |']
    out.append('|' + '|'.join(['---'] * len(headers)) + '|')
    for r in rows:
        out.append('| ' + ' | '.join(str(c) for c in r) + ' |')
    return '\n'.join(out)


def verifica_1(client, sections):
    print('VERIFICA 1 — Manodopera interna…')
    lines = client._call(
        'account.analytic.line', 'search_read',
        [['account_id', '=', ANALYTIC_ID]],
        ['id', 'date', 'name', 'unit_amount', 'amount',
         'employee_id', 'user_id', 'product_id'],
        limit=20000,
    )
    timesheet = [l for l in lines if l.get('employee_id')]
    total_hours = sum(l.get('unit_amount') or 0 for l in timesheet)
    total_amount = sum(l.get('amount') or 0 for l in timesheet)
    dates = [l['date'] for l in timesheet if l.get('date')]
    date_min = min(dates) if dates else 'N/D'
    date_max = max(dates) if dates else 'N/D'

    by_emp = defaultdict(lambda: {'hours': 0.0, 'amount': 0.0, 'name': None})
    for l in timesheet:
        emp = l.get('employee_id')
        if not emp:
            continue
        eid = emp[0]
        by_emp[eid]['name'] = emp[1]
        by_emp[eid]['hours'] += l.get('unit_amount') or 0
        by_emp[eid]['amount'] += l.get('amount') or 0

    rows = []
    for eid, d in sorted(by_emp.items(), key=lambda x: -x[1]['hours']):
        cost_h = (abs(d['amount']) / d['hours']) if d['hours'] else 0
        rows.append([d['name'], f"{d['hours']:.1f}", fmt_eur(abs(d['amount'])),
                     fmt_eur(cost_h) if cost_h else 'N/D'])

    s = []
    s.append('## VERIFICA 1 — Manodopera interna e costo del lavoro\n')
    s.append('**Sintesi**\n')
    s.append(f"- Ore timesheet totali: **{total_hours:.1f}** h")
    s.append(f"- Dipendenti distinti: **{len(by_emp)}**")
    s.append(f"- Periodo rilevazione: **{date_min} → {date_max}**")
    s.append(f"- Costo manodopera valorizzato sui timesheet: **{fmt_eur(abs(total_amount))}**")
    s.append(f"- Costo orario medio: **{fmt_eur(abs(total_amount)/total_hours)}/h**" if total_hours else "")
    s.append("\n> ⚠ **Nota contabile**: questa valorizzazione esiste solo sui timesheet (`hr.employee.timesheet_cost × ore`). NON è ribaltata sul GL contabile: il conto `440100/440200 spese del personale` non ha movimenti analitici su S03386 (sul GL solo €2.949 di note spese). Per il documento bancario è corretto considerare questi €496k come **costo industriale interno della commessa**, distinto dai costi diretti consuntivati a bilancio.\n")
    if rows:
        s.append(md_table(['Dipendente', 'Ore', 'Costo valorizzato', 'Costo orario applicato'], rows))
    else:
        s.append('_Nessun timesheet trovato sull\'analitica._')

    valorized_count = sum(1 for l in timesheet if (l.get('amount') or 0) != 0)
    if valorized_count == 0 and timesheet:
        s.append('\n> ⚠ I timesheet NON hanno valorizzazione economica (amount=0). Recupero costo orario da hr.employee.timesheet_cost…')
        emp_ids = list(by_emp.keys())
        if emp_ids:
            emps = client._call(
                'hr.employee', 'read', emp_ids,
                ['id', 'name', 'timesheet_cost'],
            )
            cost_map = {e['id']: e.get('timesheet_cost') or 0 for e in emps}
            rows2 = []
            stima_tot = 0.0
            for eid, d in sorted(by_emp.items(), key=lambda x: -x[1]['hours']):
                ch = cost_map.get(eid, 0)
                stima = d['hours'] * ch
                stima_tot += stima
                rows2.append([d['name'], f"{d['hours']:.1f}", fmt_eur(ch), fmt_eur(stima)])
            s.append('\n' + md_table(['Dipendente', 'Ore', 'Costo orario (hr.employee)', 'Stima costo'], rows2))
            s.append(f"\n**Stima costo manodopera totale: {fmt_eur(stima_tot)}**")

    sections['v1'] = {'total_hours': total_hours, 'total_amount': abs(total_amount),
                      'n_emp': len(by_emp)}
    return '\n'.join(s)


def get_amls_by_code(client, account_code):
    """Cerca account.move.line posted, analytic_account_id=3813, sui conti col codice dato.
    NB: in Ecotel ci sono account.account duplicati per multi-company → uso 'in' su TUTTI gli id.
    NB2: account.analytic.line.amount è sempre 0 in questo DB → non usabile per importi."""
    accs = client._call(
        'account.account', 'search_read',
        [['code', '=', account_code]],
        ['id', 'code', 'name', 'company_id'],
    )
    if not accs:
        return [], None
    acc_ids = [a['id'] for a in accs]
    amls = client._call(
        'account.move.line', 'search_read',
        [['analytic_account_id', '=', ANALYTIC_ID],
         ['account_id', 'in', acc_ids],
         ['parent_state', '=', 'posted']],
        ['id', 'date', 'partner_id', 'name', 'debit', 'credit',
         'move_id', 'ref', 'account_id'],
        limit=10000,
    )
    return amls, accs[0]


def verifica_locazioni_subappalti(client, code, label, sections, key):
    print(f'VERIFICA — {label} (conto {code})…')
    amls, acc = get_amls_by_code(client, code)
    if not acc:
        return f"## {label}\n\n_Conto {code} non trovato._"
    rows = []
    by_partner = defaultdict(lambda: {'amount': 0.0, 'name': None, 'count': 0})
    tot = 0.0
    for l in sorted(amls, key=lambda x: x.get('date') or ''):
        imp = (l.get('debit') or 0) - (l.get('credit') or 0)
        tot += imp
        partner = l.get('partner_id')
        pname = partner[1] if partner else '—'
        pid = partner[0] if partner else 0
        by_partner[pid]['name'] = pname
        by_partner[pid]['amount'] += imp
        by_partner[pid]['count'] += 1
        move = l.get('move_id')
        rows.append([l['date'], pname, (l.get('name') or '').replace('|', '/')[:60],
                     fmt_eur(imp), move[1] if move else '—'])

    s = []
    s.append(f'## {label}\n')
    s.append(f"**Conto {acc['code']} — {acc['name']}**\n")
    s.append(f"- Righe analitiche totali: **{len(amls)}**")
    s.append(f"- Importo complessivo: **{fmt_eur(tot)}**")
    s.append(f"- Fornitori distinti: **{len(by_partner)}**\n")
    if rows:
        s.append('**Dettaglio righe**\n')
        s.append(md_table(['Data', 'Fornitore', 'Descrizione', 'Importo', 'Fattura'], rows[:200]))
        if len(rows) > 200:
            s.append(f"\n_…troncato a 200 righe su {len(rows)} totali._")

    s.append('\n**Top fornitori per importo**\n')
    top = sorted(by_partner.items(), key=lambda x: -abs(x[1]['amount']))[:10]
    s.append(md_table(['Fornitore', '# righe', 'Importo totale'],
                      [[d['name'], d['count'], fmt_eur(d['amount'])] for _, d in top]))

    sections[key] = {'total': tot, 'n_partners': len(by_partner), 'n_rows': len(amls)}
    return '\n'.join(s)


def verifica_4(client, sections):
    print('VERIFICA 4 — PO vs Costi consuntivati…')
    pos = client._call(
        'purchase.order.line', 'search_read',
        [['account_analytic_id', '=', ANALYTIC_ID]],
        ['id', 'order_id', 'product_id', 'name',
         'product_qty', 'qty_received', 'qty_invoiced',
         'price_unit', 'price_subtotal', 'price_total',
         'date_planned', 'account_analytic_id'],
        limit=5000,
    )
    order_ids = sorted({p['order_id'][0] for p in pos if p.get('order_id')})
    orders = client._call(
        'purchase.order', 'read', order_ids,
        ['id', 'name', 'state', 'invoice_status', 'date_order',
         'partner_id', 'amount_untaxed', 'amount_total',
         'order_line', 'invoice_ids', 'company_id'],
    ) if order_ids else []

    total_po_subtotal = sum(p.get('price_subtotal') or 0 for p in pos)

    # Calcolo: per OGNI POL (anche su PO già chiusi), quanto è la quota non fatturata.
    committed_residuo = 0.0
    committed_ricevuto_non_fatturato = 0.0  # qty_received - qty_invoiced
    pol_residue_by_order = defaultdict(float)
    for p in pos:
        qty_da_fatturare = max((p.get('product_qty') or 0) - (p.get('qty_invoiced') or 0), 0)
        residuo = qty_da_fatturare * (p.get('price_unit') or 0)
        committed_residuo += residuo
        qty_recv_non_fatt = max((p.get('qty_received') or 0) - (p.get('qty_invoiced') or 0), 0)
        committed_ricevuto_non_fatturato += qty_recv_non_fatt * (p.get('price_unit') or 0)
        oid = p['order_id'][0]
        pol_residue_by_order[oid] += residuo

    not_invoiced = []
    for o in orders:
        residuo = pol_residue_by_order.get(o['id'], 0)
        if residuo <= 0.01 and o['invoice_status'] == 'invoiced':
            continue
        if residuo <= 0.01:
            continue
        not_invoiced.append({
            'name': o['name'],
            'partner': o['partner_id'][1] if o.get('partner_id') else '—',
            'state': o['state'],
            'invoice_status': o['invoice_status'],
            'residuo': residuo,
            'date_order': o['date_order'],
        })

    not_invoiced.sort(key=lambda x: -x['residuo'])

    sanity_anomalie = []
    for p in pos:
        oid = p.get('order_id')[0] if p.get('order_id') else None
        for o in orders:
            if o['id'] == oid:
                break

    s = []
    s.append('## VERIFICA 4 — Differenza PO vs Costi consuntivati\n')
    s.append('**Sintesi PO sull\'analitica**\n')
    s.append(f"- PO line totali su analitica 3813: **{len(pos)}**")
    s.append(f"- Purchase order distinti: **{len(orders)}**")
    s.append(f"- Imponibile complessivo POL: **{fmt_eur(total_po_subtotal)}**")
    s.append(f"- PO con residuo da fatturare > 0: **{len(not_invoiced)}**")
    s.append(f"- Committed cost residuo TOTALE (qty ordinata − qty fatturata, valorizzato): **{fmt_eur(committed_residuo)}**")
    s.append(f"- Di cui ricevuto ma non ancora fatturato (merce/servizi consegnati): **{fmt_eur(committed_ricevuto_non_fatturato)}**\n")

    if not_invoiced:
        s.append('**PO con residuo da fatturare (top 30)**\n')
        s.append(md_table(['PO', 'Fornitore', 'Stato PO', 'Stato fatt.', 'Residuo', 'Data ordine'],
                          [[n['name'], n['partner'], n['state'], n['invoice_status'],
                            fmt_eur(n['residuo']), n['date_order']]
                           for n in not_invoiced[:30]]))

    sections['v4'] = {
        'n_pol': len(pos),
        'n_po': len(orders),
        'tot_pol_subtotal': total_po_subtotal,
        'committed_residuo': committed_residuo,
        'committed_ricevuto_non_fatturato': committed_ricevuto_non_fatturato,
        'po_not_invoiced': len(not_invoiced),
    }
    return '\n'.join(s)


def verifica_6_extra_oneri_factor(client):
    """Cerca scritture su conti di oneri/sconti cessione/factoring relativi a S03386."""
    candidate_codes = []
    accs = client._call(
        'account.account', 'search_read',
        ['|', '|', '|', '|',
         ['name', 'ilike', 'cession'],
         ['name', 'ilike', 'factor'],
         ['name', 'ilike', 'sconto'],
         ['name', 'ilike', 'smobiliz'],
         ['code', '=like', '415%']],
        ['id', 'code', 'name'],
        limit=500,
    )
    if not accs:
        return None, []
    acc_ids = [a['id'] for a in accs]

    # Cerca account.analytic.line su questi conti per analitica 3813
    alines = client._call(
        'account.analytic.line', 'search_read',
        [['account_id', '=', ANALYTIC_ID],
         ['general_account_id', 'in', acc_ids]],
        ['id', 'date', 'partner_id', 'name', 'amount',
         'move_id', 'general_account_id'],
        limit=2000,
    )
    return accs, alines


def _tax_regime(tax_names):
    """Inferisce regime IVA dal nome della tax applicata."""
    if not tax_names:
        return 'esente / nessuna tax'
    blob = ' | '.join(tax_names).lower()
    if 'reverse' in blob or 'rev. ' in blob or 'reverse charge' in blob or 'art.17' in blob:
        return 'reverse charge'
    if 'split' in blob or 'scissione' in blob:
        return 'split payment'
    if '22%' in blob or '22 %' in blob or 'iva 22' in blob:
        return 'ordinario 22%'
    if '10%' in blob:
        return 'ordinario 10%'
    if '4%' in blob:
        return 'ordinario 4%'
    if 'esente' in blob or 'art.10' in blob or 'art. 10' in blob:
        return 'esente'
    if 'non sogg' in blob or 'fuori campo' in blob:
        return 'fuori campo'
    return f'altro ({tax_names[0] if tax_names else "?"})'


def verifica_5_6(client, sections):
    print('VERIFICA 5+6 — Fatture cliente, IVA, incassi, sconto cessione…')
    so = client._call('sale.order', 'read', [SALE_ORDER_ID],
                      ['id', 'name', 'invoice_ids', 'amount_total', 'amount_untaxed'])
    inv_ids = so[0]['invoice_ids']
    invoices = client._call(
        'account.move', 'read', inv_ids,
        ['id', 'name', 'move_type', 'invoice_date', 'partner_id',
         'amount_untaxed_signed', 'amount_tax_signed', 'amount_total_signed',
         'amount_untaxed', 'amount_tax', 'amount_total', 'amount_residual',
         'state', 'fiscal_position_id', 'invoice_line_ids',
         'invoice_payments_widget'],
    )
    out_invoices = [i for i in invoices if i['move_type'] == 'out_invoice']

    line_ids = []
    for i in out_invoices:
        line_ids.extend(i['invoice_line_ids'])
    aml_lines = client._call(
        'account.move.line', 'read', line_ids,
        ['id', 'move_id', 'tax_ids', 'price_subtotal', 'price_total',
         'account_id'],
    ) if line_ids else []

    tax_ids_all = set()
    for l in aml_lines:
        for t in l.get('tax_ids') or []:
            tax_ids_all.add(t)
    taxes = client._call('account.tax', 'read', list(tax_ids_all),
                        ['id', 'name', 'amount', 'amount_type', 'price_include']) if tax_ids_all else []
    tax_name_map = {t['id']: t['name'] for t in taxes}

    rows5 = []
    by_inv_taxes = defaultdict(set)
    for l in aml_lines:
        mid = l['move_id'][0]
        for t in l.get('tax_ids') or []:
            by_inv_taxes[mid].add(tax_name_map.get(t, str(t)))

    tot_netto = 0.0
    tot_iva = 0.0
    tot_lordo = 0.0
    for inv in sorted(out_invoices, key=lambda x: x.get('invoice_date') or ''):
        netto = inv['amount_untaxed']
        iva = inv['amount_tax']
        lordo = inv['amount_total']
        tot_netto += netto
        tot_iva += iva
        tot_lordo += lordo
        tax_list = sorted(by_inv_taxes.get(inv['id'], []))
        regime = _tax_regime(tax_list)
        rows5.append([inv['name'], inv.get('invoice_date') or 'N/D',
                      fmt_eur(netto), fmt_eur(iva), fmt_eur(lordo),
                      regime + (f" — _{', '.join(tax_list)}_" if tax_list else '')])

    rows5.append(['**TOTALE**', '', f"**{fmt_eur(tot_netto)}**", f"**{fmt_eur(tot_iva)}**",
                  f"**{fmt_eur(tot_lordo)}**", ''])

    s5 = []
    s5.append('## VERIFICA 5 — Ricavi: composizione IVA per fattura\n')
    s5.append(f"Fatture attive collegate a SO `{SO_NAME}` (out_invoice): **{len(out_invoices)}**\n")
    s5.append(md_table(['Fattura', 'Data', 'Netto', 'IVA', 'Lordo', 'Regime IVA'], rows5))

    rows6 = []
    sum_lordo = 0.0
    sum_incassato = 0.0
    sum_sconto = 0.0
    for inv in sorted(out_invoices, key=lambda x: x.get('invoice_date') or ''):
        widget_raw = inv.get('invoice_payments_widget')
        payments = []
        if widget_raw and widget_raw != 'false':
            try:
                w = json.loads(widget_raw) if isinstance(widget_raw, str) else widget_raw
                payments = (w or {}).get('content', []) or []
            except Exception:
                payments = []
        incassato = sum((p.get('amount') or 0) for p in payments)
        dates_pay = [p.get('date') for p in payments if p.get('date')]
        data_cessione = min(dates_pay) if dates_pay else 'N/D'

        tax_list = sorted(by_inv_taxes.get(inv['id'], []))
        regime = _tax_regime(tax_list)
        base_smobilizzo = inv['amount_untaxed'] if regime == 'reverse charge' else inv['amount_total']

        sconto = base_smobilizzo - incassato
        pct = (sconto / base_smobilizzo * 100) if base_smobilizzo else 0
        sum_lordo += base_smobilizzo
        sum_incassato += incassato
        sum_sconto += sconto
        rows6.append([inv['name'], regime,
                      fmt_eur(base_smobilizzo), fmt_eur(incassato),
                      fmt_eur(sconto), fmt_pct(pct), data_cessione])

    rows6.append(['**TOTALE**', '', f"**{fmt_eur(sum_lordo)}**",
                  f"**{fmt_eur(sum_incassato)}**", f"**{fmt_eur(sum_sconto)}**",
                  f"**{fmt_pct((sum_sconto/sum_lordo*100) if sum_lordo else 0)}**", ''])

    s6 = []
    s6.append('## VERIFICA 6 — Incassi reali: sconto cessione del credito\n')
    s6.append('Per ogni fattura: lordo (o netto se reverse charge), incassato dai pagamenti riconciliati, delta = sconto cessione.\n')
    s6.append(md_table(['Fattura', 'Regime', 'Base smobilizzo', 'Incassato', 'Sconto cessione', 'Sconto %', 'Data 1° pagamento'], rows6))

    # Cerca oneri di cessione/factoring imputati sull'analitica
    s6.append('\n### Ricerca scritture oneri di cessione/factoring sull\'analitica 3813\n')
    accs, alines_factor = verifica_6_extra_oneri_factor(client)
    if accs:
        s6.append(f"Conti candidati (codice 415xxx o nome contenente cessione/factor/sconto/smobiliz): **{len(accs)}**")
        if alines_factor:
            tot_oneri = sum(-(l.get('amount') or 0) for l in alines_factor)
            rows_factor = []
            for l in sorted(alines_factor, key=lambda x: x.get('date') or ''):
                acc = l.get('general_account_id')
                partner = l.get('partner_id')
                rows_factor.append([l['date'], acc[1] if acc else '—',
                                    partner[1] if partner else '—',
                                    (l.get('name') or '')[:60],
                                    fmt_eur(-(l.get('amount') or 0))])
            s6.append(f"Scritture trovate sull\'analitica 3813: **{len(alines_factor)}** — Totale: **{fmt_eur(tot_oneri)}**\n")
            s6.append(md_table(['Data', 'Conto', 'Controparte', 'Descrizione', 'Importo'], rows_factor[:50]))
        else:
            s6.append('_Nessuna scrittura su questi conti risulta imputata sull\'analitica 3813._')
            s6.append('\n> ⚠ **Implicazione**: lo sconto cessione del credito potrebbe non essere ribaltato sull\'analitica della commessa. Andrebbe quantificato a livello aziendale (non per commessa) e poi attribuito proporzionalmente. Se invece le 3 fatture non incassate (€860.458) non sono ancora state cedute al factoring, il loro 100% di "sconto" calcolato in tabella è un artefatto del filtro pagamenti — non un costo reale.')
    else:
        s6.append('_Nessun conto candidato trovato._')

    sections['v5'] = {'tot_netto': tot_netto, 'tot_iva': tot_iva, 'tot_lordo': tot_lordo,
                      'n_fatture': len(out_invoices)}
    sections['v6'] = {'sum_lordo': sum_lordo, 'sum_incassato': sum_incassato,
                      'sum_sconto': sum_sconto,
                      'pct_medio': (sum_sconto/sum_lordo*100) if sum_lordo else 0}

    return '\n'.join(s5) + '\n\n---\n\n' + '\n'.join(s6)


def sintesi_finale(sections):
    s = []
    s.append('## Sintesi per il documento bancario\n')
    v1 = sections.get('v1', {})
    v2 = sections.get('v2', {})
    v3 = sections.get('v3', {})
    v4 = sections.get('v4', {})
    v5 = sections.get('v5', {})
    v6 = sections.get('v6', {})

    ricavi = v5.get('tot_netto', 0)
    costi_diretti = v2.get('total', 0) + v3.get('total', 0)
    manodopera = v1.get('total_amount', 0)
    margine_lordo = ricavi - costi_diretti
    margine_rettificato = ricavi - costi_diretti - manodopera

    s.append('| Numero chiave | Valore |')
    s.append('|---|---|')
    s.append(f"| Ricavi commessa (netto fatturato) | {fmt_eur(ricavi)} |")
    s.append(f"| Costi diretti — Locazioni 430100 | {fmt_eur(v2.get('total', 0))} |")
    s.append(f"| Costi diretti — Subappalti 420180 | {fmt_eur(v3.get('total', 0))} |")
    s.append(f"| **Margine lordo (ricavi − locazioni − subappalti)** | **{fmt_eur(margine_lordo)} ({fmt_pct(margine_lordo/ricavi*100 if ricavi else 0)})** |")
    s.append(f"| Manodopera interna (timesheet valorizzato, non ribaltata in GL) | {fmt_eur(manodopera)} |")
    s.append(f"| **Margine rettificato (− manodopera interna)** | **{fmt_eur(margine_rettificato)} ({fmt_pct(margine_rettificato/ricavi*100 if ricavi else 0)})** |")
    s.append(f"| Committed cost residuo PO (impegno non ancora consuntivato) | {fmt_eur(v4.get('committed_residuo', 0))} |")
    s.append(f"| Volume fatturato cliente (lordo + reverse charge a netto) | {fmt_eur(v6.get('sum_lordo', 0))} |")
    s.append(f"| Incassato effettivo (pagamenti riconciliati) | {fmt_eur(v6.get('sum_incassato', 0))} |")
    s.append(f"| Esposizione attuale (fatturato − incassato) | {fmt_eur(v6.get('sum_lordo', 0) - v6.get('sum_incassato', 0))} |")

    s.append('\n**Nota sullo sconto cessione**: dall\'analisi dei pagamenti Odoo riconciliati, le 10 fatture incassate (€1.315k) risultano riconciliate al **100% del valore lordo** — quindi gli **oneri da cessione non sono ribaltati sull\'analitica della commessa** e non sono leggibili da Odoo per S03386. La quantificazione del costo cessione va fatta separatamente, su base aziendale (estratto conto cassa vs. fatture incassate, oppure scritture su 415xxx oneri bancari/factoring).')
    s.append('\n**3 fatture non ancora incassate (€860.458)**:\n')
    s.append('- 2026-0562 (€236.653) — 31/03/2026')
    s.append('- 2026-0574 (€4.148) — 31/03/2026')
    s.append('- 2026-0833 (€619.657) — 13/05/2026 (recentissima)')
    s.append('\nQuesti importi rappresentano l\'esposizione **attiva** non ancora smobilizzata, non un costo. Sarebbero candidati ideali per il nuovo affidamento.')
    return '\n'.join(s)


def main():
    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()
    print(f'Connesso. Analitica {ANALYTIC_ID} (S03386), SO id {SALE_ORDER_ID}.\n')

    sections = {}

    parts = []
    parts.append(f"# Audit commessa S03386 — WE4SERVICES S.C. A R.L.")
    parts.append(f"\n_Generato: {datetime.now().strftime('%Y-%m-%d %H:%M')} — DB: `{os.environ['ODOO_DB']}` — analytic_id: {ANALYTIC_ID} — sale_order_id: {SALE_ORDER_ID}_\n")
    parts.append('---\n')

    parts.append(verifica_1(client, sections))
    parts.append('\n---\n')
    parts.append(verifica_locazioni_subappalti(client, '430100', 'VERIFICA 2 — Locazioni passive (conto 430100)', sections, 'v2'))
    parts.append('\n---\n')
    parts.append(verifica_locazioni_subappalti(client, '420180', 'VERIFICA 3 — Subappalti e lavorazioni terzi (conto 420180)', sections, 'v3'))
    parts.append('\n---\n')
    parts.append(verifica_4(client, sections))
    parts.append('\n---\n')
    parts.append(verifica_5_6(client, sections))
    parts.append('\n---\n')
    parts.append(sintesi_finale(sections))

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text('\n'.join(parts), encoding='utf-8')
    print(f"\nReport scritto in: {OUTPUT_PATH}")


if __name__ == '__main__':
    main()
