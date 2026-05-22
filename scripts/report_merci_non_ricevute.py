"""
Report serale: fatture in fatturapa.attachment.in (registered=False) per le
quali la merce NON e' stata ancora ricevuta al magazzino Ecotel.

Logica:
1. Carico tutti gli attachment registered=False di Ecotel (company_id=1, no
   self_invoice) degli ultimi 90 giorni.
2. Parso l'XML per estrarre numero/data/totale/cedente/oda_riferimenti.
3. Per ogni OdA citato nell'XML (o dedotto da partner+importo), recupero le
   purchase.order.line e verifico:
     - product.type IN ('product','consu')  -> riga MERCE
     - qty_received == 0                    -> NON ricevuta
4. Tiro fuori SOLO le fatture che hanno almeno 1 riga merce con qty_received=0.
5. Aggrego per fornitore, ordinato per giorni di ritardo decrescenti.
6. Invio mail HTML schematica.

Schedulazione: Task Scheduler Windows giornaliero alle 19:30 sul server.

Lanciabile a mano: `python scripts/report_merci_non_ricevute.py`
Opzioni:
  --dry-run         non invia mail, stampa HTML su stdout
  --lookback N      sovrascrive lookback default 90 giorni
  --output FILE     scrive HTML su file (per debug)
"""
from __future__ import annotations
import sys
import os
import re
import argparse
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.fatturapa_parser import parse_from_base64

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger('report_merci')


# ============================================================
# DATACLASSES
# ============================================================

@dataclass
class PolRow:
    pol_id: int
    product_name: str
    product_type: str           # 'product' (stoccabile), 'consu' (consumabile), 'service'
    qty_ordered: float
    qty_received: float
    qty_invoiced: float
    price_unit: float
    is_merce: bool              # True se product/consu, False se service


@dataclass
class PoData:
    po_id: int
    name: str
    state: str
    invoice_status: str
    date_order: str
    origin: str
    picking_count: int
    pickings: List[Dict] = field(default_factory=list)  # subset stock.picking
    pol_rows: List[PolRow] = field(default_factory=list)


@dataclass
class InvoiceRecord:
    attachment_id: int
    invoice_number: str
    invoice_date: str           # YYYY-MM-DD
    cedente: str
    cedente_vat: str
    importo_totale: float
    create_date: str            # data ricezione SdI
    oda_refs: List[str]         # OdA da XML (oda_riferimenti)
    oda_resolved: List[PoData]  # OdA confermati su Odoo


# ============================================================
# QUERY
# ============================================================

def fetch_pending_attachments(client: OdooReadOnlyClient,
                              lookback_days: int) -> List[Dict]:
    """Fatturapa attachments non-registered di Ecotel, no autofatture, ultimi N gg."""
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    return client._call(
        'fatturapa.attachment.in', 'search_read',
        [('registered', '=', False),
         ('is_self_invoice', '=', False),
         ('company_id', '=', 1),
         ('create_date', '>=', cutoff)],
        fields=['id', 'att_name', 'xml_supplier_id',
                'invoices_total', 'invoices_date',
                'create_date'],
        order='create_date asc',
        limit=5000,
    )


def fetch_po_by_names(client: OdooReadOnlyClient, names: Set[str]) -> Dict[str, Dict]:
    """Ritorna dict name->purchase.order per i nomi richiesti."""
    if not names:
        return {}
    pos = client._call(
        'purchase.order', 'search_read',
        [('name', 'in', list(names))],
        fields=['id', 'name', 'state', 'invoice_status',
                'date_order', 'origin', 'picking_count',
                'picking_ids', 'order_line', 'partner_id'],
        limit=2000,
    )
    return {p['name']: p for p in pos}


def fetch_pol_with_product_type(client: OdooReadOnlyClient,
                                pol_ids: List[int]) -> Dict[int, PolRow]:
    """Carica POL + product.type per ogni id richiesto."""
    if not pol_ids:
        return {}
    pol = client._call(
        'purchase.order.line', 'read', pol_ids,
        fields=['id', 'name', 'product_id', 'product_qty',
                'qty_received', 'qty_invoiced', 'price_unit'])
    # raccolgo product_ids
    prod_ids = []
    for ln in pol:
        p = ln.get('product_id')
        if p:
            prod_ids.append(p[0])
    prod_ids = list(set(prod_ids))
    products = {}
    if prod_ids:
        rows = client._call(
            'product.product', 'read', prod_ids,
            fields=['id', 'type'])
        for r in rows:
            products[r['id']] = r.get('type', '')
    result = {}
    for ln in pol:
        p = ln.get('product_id')
        pid = p[0] if p else None
        ptype = products.get(pid, '') if pid else ''
        prod_name = p[1] if p else ''
        is_merce = ptype in ('product', 'consu')
        result[ln['id']] = PolRow(
            pol_id=ln['id'],
            product_name=prod_name,
            product_type=ptype,
            qty_ordered=ln.get('product_qty') or 0,
            qty_received=ln.get('qty_received') or 0,
            qty_invoiced=ln.get('qty_invoiced') or 0,
            price_unit=ln.get('price_unit') or 0,
            is_merce=is_merce,
        )
    return result


def fetch_pickings(client: OdooReadOnlyClient,
                   picking_ids: List[int]) -> Dict[int, Dict]:
    if not picking_ids:
        return {}
    rows = client._call(
        'stock.picking', 'read', picking_ids,
        fields=['id', 'name', 'state', 'scheduled_date', 'date_done'])
    return {r['id']: r for r in rows}


# ============================================================
# CORE
# ============================================================

def analyze(client: OdooReadOnlyClient,
            lookback_days: int) -> List[InvoiceRecord]:
    """Cuore del report: identifica le fatture con merci NON ricevute."""
    logger.info(f"Carico attachment non-registered ultimi {lookback_days} gg")
    atts = fetch_pending_attachments(client, lookback_days)
    logger.info(f"Trovati: {len(atts)} attachment")

    invoices: List[InvoiceRecord] = []
    referenced_oda_names: Set[str] = set()
    att_to_parsed: Dict[int, object] = {}

    # 1. Parse XML di tutti gli attachment + colleziono OdA citati
    for a in atts:
        full = client._call('fatturapa.attachment.in', 'read', [a['id']],
                          fields=['datas'])
        if not full or not full[0].get('datas'):
            continue
        try:
            parsed = parse_from_base64(full[0]['datas'])
        except Exception as e:
            logger.warning(f"parse error att {a['id']}: {e}")
            continue
        att_to_parsed[a['id']] = parsed
        for oda in parsed.oda_riferimenti or []:
            referenced_oda_names.add(oda.strip())

    logger.info(f"OdA distinti citati negli XML: {len(referenced_oda_names)}")

    # 2. Bulk fetch PO per name
    po_by_name = fetch_po_by_names(client, referenced_oda_names)
    logger.info(f"OdA trovati su Odoo: {len(po_by_name)}")

    # 3. Bulk fetch POL e Pickings necessari
    all_pol_ids: List[int] = []
    all_picking_ids: List[int] = []
    for po in po_by_name.values():
        all_pol_ids.extend(po.get('order_line') or [])
        all_picking_ids.extend(po.get('picking_ids') or [])
    pol_by_id = fetch_pol_with_product_type(client, all_pol_ids)
    picking_by_id = fetch_pickings(client, all_picking_ids)

    # 4. Build InvoiceRecord per ogni attachment, applico filtro merci NON ricevute
    for a in atts:
        parsed = att_to_parsed.get(a['id'])
        if not parsed:
            continue
        oda_refs = parsed.oda_riferimenti or []
        if not oda_refs:
            # Skip: senza OdA esplicito non possiamo verificare picking
            # (il match implicito/parziale lo fa il classifier; il report
            # serale resta conservativo).
            continue

        resolved_pos: List[PoData] = []
        for oda_name in oda_refs:
            po = po_by_name.get(oda_name.strip())
            if not po:
                continue
            pol_ids = po.get('order_line') or []
            picking_ids = po.get('picking_ids') or []
            pol_rows = [pol_by_id[pid] for pid in pol_ids if pid in pol_by_id]
            pickings = [picking_by_id[pid] for pid in picking_ids if pid in picking_by_id]
            resolved_pos.append(PoData(
                po_id=po['id'], name=po['name'], state=po.get('state', ''),
                invoice_status=po.get('invoice_status', ''),
                date_order=po.get('date_order', ''),
                origin=po.get('origin', '') or '',
                picking_count=po.get('picking_count', 0) or 0,
                pickings=pickings, pol_rows=pol_rows,
            ))

        if not resolved_pos:
            continue

        # Filtro: tieni fattura SOLO se almeno 1 POL merce su almeno 1 OdA
        # ha qty_received=0
        keep = False
        for podata in resolved_pos:
            merci_rows = [pr for pr in podata.pol_rows if pr.is_merce]
            if not merci_rows:
                continue
            unreceived = [pr for pr in merci_rows if pr.qty_received == 0]
            if unreceived:
                keep = True
                break
        if not keep:
            continue

        # Cedente
        ced_name = parsed.cedente_denominazione or ''
        sup = a.get('xml_supplier_id')
        if not ced_name and sup:
            ced_name = sup[1]
        # importo: parse > attachment cache
        importo = parsed.importo_totale or (a.get('invoices_total') or 0)
        # data ft: parse > attachment (formato DD/MM/YYYY -> normalizzo)
        inv_date = parsed.data or ''
        if not inv_date:
            raw = a.get('invoices_date') or ''
            if raw and '/' in raw:
                parts = raw.split('/')
                if len(parts) == 3:
                    inv_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
        invoices.append(InvoiceRecord(
            attachment_id=a['id'],
            invoice_number=parsed.numero or '',
            invoice_date=inv_date,
            cedente=ced_name,
            cedente_vat=parsed.cedente_partita_iva or '',
            importo_totale=float(importo),
            create_date=(a.get('create_date') or '')[:19],
            oda_refs=oda_refs,
            oda_resolved=resolved_pos,
        ))

    return invoices


# ============================================================
# RENDER HTML
# ============================================================

def _days_since(date_str: str) -> Optional[int]:
    if not date_str or '-' not in date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
        return (date.today() - d).days
    except Exception:
        return None


def _fmt_eur(x: float) -> str:
    return f"€ {x:,.2f}".replace(',', '_').replace('.', ',').replace('_', '.')


def render_html(invoices: List[InvoiceRecord], lookback_days: int) -> str:
    today_str = date.today().strftime('%d/%m/%Y')
    # Aggrega per fornitore
    by_supplier: Dict[str, List[InvoiceRecord]] = defaultdict(list)
    for inv in invoices:
        by_supplier[inv.cedente or '(senza nome)'].append(inv)

    n_ft = len(invoices)
    n_for = len(by_supplier)
    tot_amt = sum(i.importo_totale for i in invoices)

    # Ordina fornitori per importo totale decrescente
    sorted_suppliers = sorted(by_supplier.items(),
                              key=lambda kv: sum(i.importo_totale for i in kv[1]),
                              reverse=True)

    parts = []
    parts.append("""<html><head><meta charset="utf-8"><style>
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:13px;color:#222;max-width:1100px;margin:20px auto;padding:0 20px}
h1{font-size:18px;color:#a30000;border-bottom:2px solid #a30000;padding-bottom:6px;margin-bottom:6px}
h2{font-size:14px;color:#333;background:#f4f4f4;padding:6px 10px;margin-top:20px;border-left:4px solid #c00}
.summary{background:#fff3cd;border:1px solid #ffe69c;padding:10px;border-radius:4px;margin:14px 0}
table{border-collapse:collapse;width:100%;margin:6px 0 12px 0;font-size:12px}
th{background:#e9ecef;border:1px solid #ccc;padding:5px 7px;text-align:left;font-weight:600}
td{border:1px solid #ddd;padding:4px 7px;vertical-align:top}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.rit-alta{color:#a30000;font-weight:700}
.rit-media{color:#b85a00}
.rit-bassa{color:#666}
.small{color:#888;font-size:11px}
.muted{color:#888}
.pol-merce{background:#ffebeb}
.pol-ricevuta{color:#666}
.footer{margin-top:30px;border-top:1px solid #ddd;padding-top:10px;color:#888;font-size:11px}
</style></head><body>""")

    parts.append(f"<h1>📦 Fatture ferme su merci non ricevute</h1>")
    parts.append(f'<div class="small">Report del {today_str} · '
                 f'lookback {lookback_days} giorni · '
                 f'Ecotel Italia · no autofatture</div>')

    parts.append(f'<div class="summary">'
                 f'<b>{n_ft} fatture</b> da {n_for} fornitori · '
                 f'totale a rischio: <b>{_fmt_eur(tot_amt)}</b>'
                 f'</div>')

    if not invoices:
        parts.append('<p style="color:#0a0;font-size:14px">'
                    'Tutto in ordine: nessuna fattura in attesa con merci non ricevute.'
                    '</p>')
    else:
        parts.append(
            '<p style="margin:6px 0">Fatture ricevute via SdI per le quali '
            "almeno una riga merce sull'OdA collegato risulta "
            '<b>qty_received = 0</b> '
            '(picking di ricezione mai validato o materiale non ancora '
            "consegnato al magazzino Ecotel). Dal giorno dell'emissione "
            'partono i termini di pagamento — necessario allineamento con '
            'logistica o fornitore.</p>'
        )

        for supplier, invs in sorted_suppliers:
            tot_sup = sum(i.importo_totale for i in invs)
            parts.append(f'<h2>🏷 {supplier}'
                         f' <span class="small">· {len(invs)} ft · '
                         f'totale {_fmt_eur(tot_sup)}</span></h2>')
            # Tabella sintetica
            parts.append('<table>'
                         '<tr>'
                         '<th>Fattura</th><th>Data emissione</th>'
                         '<th>Ricevuta SdI</th><th>Giorni</th>'
                         '<th class="num">Totale</th>'
                         '<th>OdA</th>'
                         '<th>Picking</th>'
                         '<th>Righe merce non ricevute</th>'
                         '</tr>')
            for inv in sorted(invs, key=lambda i: i.invoice_date):
                gg = _days_since(inv.invoice_date)
                if gg is None:
                    gg_cls = ''
                    gg_str = '?'
                elif gg >= 30:
                    gg_cls = 'rit-alta'
                    gg_str = f"{gg} gg"
                elif gg >= 14:
                    gg_cls = 'rit-media'
                    gg_str = f"{gg} gg"
                else:
                    gg_cls = 'rit-bassa'
                    gg_str = f"{gg} gg"

                # Aggrego OdA + picking + righe merce non ricevute
                oda_str_parts = []
                picking_str_parts = []
                merci_rows = []
                for podata in inv.oda_resolved:
                    oda_str_parts.append(
                        f"<b>{podata.name}</b>"
                        f'<div class="small">{podata.origin or ""}</div>'
                    )
                    if not podata.pickings:
                        picking_str_parts.append(
                            '<span class="rit-alta">nessun picking</span>')
                    for pk in podata.pickings:
                        st = pk.get('state', '?')
                        done = pk.get('date_done')
                        if st == 'done' and done:
                            cls = 'pol-ricevuta'
                            label = f"{pk['name']}: <b>done</b> {str(done)[:10]}"
                        else:
                            cls = 'rit-alta'
                            label = f"{pk['name']}: <b>{st}</b>"
                            if pk.get('scheduled_date'):
                                label += f' <span class="small">'\
                                        f'sched {str(pk["scheduled_date"])[:10]}</span>'
                        picking_str_parts.append(
                            f'<div class="{cls}">{label}</div>')

                    for pr in podata.pol_rows:
                        if pr.is_merce and pr.qty_received == 0:
                            merci_rows.append(pr)

                rows_str_parts = []
                for pr in merci_rows[:8]:
                    rows_str_parts.append(
                        f"<div>qty {pr.qty_ordered:g} × "
                        f"<span class='small'>"
                        f"({pr.product_type})</span> "
                        f"{pr.product_name[:70]}</div>"
                    )
                if len(merci_rows) > 8:
                    rows_str_parts.append(
                        f'<div class="small">+ altre {len(merci_rows)-8} righe</div>')
                if not rows_str_parts:
                    rows_str_parts.append('<span class="muted">—</span>')

                parts.append('<tr class="pol-merce">'
                             f'<td>{inv.invoice_number}</td>'
                             f'<td>{inv.invoice_date}</td>'
                             f'<td>{inv.create_date[:10]}</td>'
                             f'<td class="{gg_cls}">{gg_str}</td>'
                             f'<td class="num">{_fmt_eur(inv.importo_totale)}</td>'
                             f'<td>{"".join(oda_str_parts)}</td>'
                             f'<td>{"".join(picking_str_parts)}</td>'
                             f'<td>{"".join(rows_str_parts)}</td>'
                             '</tr>')
            parts.append('</table>')

    parts.append('<div class="footer">'
                 'Report generato automaticamente dall\'Agent Fatturazione '
                 'Passiva · invoice agent Ecotel · '
                 'Solo fatture <b>registered=False</b> con almeno una riga '
                 "merce su OdA citato esplicitamente nell'XML."
                 '</div></body></html>')
    return ''.join(parts)


# ============================================================
# MAIL
# ============================================================

def send_mail(html: str, subject: str) -> None:
    host = os.environ.get('SMTP_HOST')
    port_str = os.environ.get('SMTP_PORT', '587')
    user = os.environ.get('SMTP_USER')
    password = os.environ.get('SMTP_PASS')
    sender = os.environ.get('SMTP_FROM') or user
    to_str = os.environ.get('SMTP_TO_REPORT_PICKING', '')
    if not (host and user and password and to_str):
        raise RuntimeError(
            "Configurazione SMTP incompleta: verificare SMTP_HOST, "
            "SMTP_USER, SMTP_PASS, SMTP_TO_REPORT_PICKING in credentials.env"
        )
    port = int(port_str)
    to_list = [t.strip() for t in to_str.split(',') if t.strip()]

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ', '.join(to_list)
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    logger.info(f"SMTP connect {host}:{port}")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(user, password)
        smtp.sendmail(sender, to_list, msg.as_string())
    logger.info(f"Mail inviata a {to_list}")


# ============================================================
# MAIN
# ============================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--lookback', type=int, default=90,
                    help='giorni di lookback (default 90)')
    ap.add_argument('--dry-run', action='store_true',
                    help='non invia mail, stampa solo')
    ap.add_argument('--output', type=str,
                    help='scrive HTML su file (per debug)')
    args = ap.parse_args()

    client = OdooReadOnlyClient(
        url=os.environ['ODOO_URL'],
        db=os.environ['ODOO_DB'],
        username=os.environ['ODOO_USERNAME'],
        password=os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    invoices = analyze(client, args.lookback)
    logger.info(f"Fatture con merci non ricevute: {len(invoices)}")

    html = render_html(invoices, args.lookback)

    if args.output:
        Path(args.output).write_text(html, encoding='utf-8')
        logger.info(f"HTML salvato in {args.output}")

    today_str = date.today().strftime('%d/%m/%Y')
    subject = (f"[Agent fatture] Merci non ricevute · {len(invoices)} ft "
               f"· {today_str}")

    if args.dry_run:
        logger.info("DRY-RUN: mail NON inviata. Soggetto:")
        logger.info(f"  {subject}")
        # piccolo riepilogo testo
        from collections import defaultdict
        by_sup = defaultdict(list)
        for inv in invoices:
            by_sup[inv.cedente].append(inv)
        for sup, invs in sorted(by_sup.items(),
                                key=lambda kv: -sum(i.importo_totale for i in kv[1])):
            print(f"\n=== {sup} ({len(invs)} ft, "
                  f"€{sum(i.importo_totale for i in invs):.2f}) ===")
            for inv in invs:
                gg = _days_since(inv.invoice_date)
                print(f"  {inv.invoice_number:<22} | data {inv.invoice_date} "
                      f"| gg {gg} | tot €{inv.importo_totale:.2f} "
                      f"| OdA {','.join(inv.oda_refs)}")
        return

    if not invoices:
        logger.info("Nessuna fattura in attesa con merci non ricevute. "
                    "Mail comunque inviata (status check).")
    send_mail(html, subject)


if __name__ == '__main__':
    main()
