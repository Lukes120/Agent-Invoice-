"""ANALISI READONLY delle fatture NO_ODA_CON_SUGGERIMENTI.

Per ciascuna fattura classificata NO_ODA_CON_SUGGERIMENTI dalla pipeline standard
(stessi parametri di Agent Invoices/app.py), interroga gli OdA APERTI del fornitore
creati negli ULTIMI 2 MESI e calcola, per ogni OdA candidato, le evidenze di
abbinabilita':
  - IMPORTO   : esiste un sottoinsieme di righe OdA che somma l'imponibile fattura?
  - COD.ART.  : quota di righe fattura il cui codice articolo (sole cifre) compare
                nel name della riga OdA tra [...].
  - DESCR.    : massima similarita' descrizione riga-fattura vs riga-OdA (difflib).
  - COMMESSA  : la commessa S##### della fattura compare nell'origin dell'OdA?

NESSUNA SCRITTURA. Solo letture Odoo. Output a video.

Uso:
    python scripts/diag_no_oda_con_suggerimenti.py
    python scripts/diag_no_oda_con_suggerimenti.py --months 2
"""
import os
import re
import sys
import argparse
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_client import OdooReadOnlyClient
from core.matcher import InvoiceMatcher
from core.fatturapa_analyzer import FatturaPAAnalyzer
from core.keyword_rules import classify_line_by_keyword
from config.rules import (
    TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA, TOLLERANZA_TOTALE_FATTURA,
    TOLLERANZA_MATCH_IMPLICITO_PERCENT, TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
    TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA, TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
    MATCH_IMPLICITO_GUARDIA_DUPLICATI, MATCH_IMPLICITO_ATTIVO,
    MATCH_PARZIALE_ATTIVO, MATCH_PARZIALE_MAX_RIGHE,
    MATCH_PARZIALE_MAX_EXTRA_PERCENT, MATCH_PARZIALE_TOLLERANZA_ASSOLUTA,
    SUGGERIMENTI_ATTIVI, SUGGERIMENTI_MAX_RIGHE,
    SUGGERIMENTI_TOLLERANZA_ASSOLUTA, SUGGERIMENTI_MAX_AGE_MONTHS,
    MAPPATURA_FORNITORI_FISSI, MAPPATURA_FORNITORI_FISSI_ATTIVA,
)

TARGET_CLASS = "NO_ODA_CON_SUGGERIMENTI"


def only_digits(s):
    return re.sub(r'\D', '', s or '')


def codes_in_po_name(name):
    """Estrae i codici tra [...] dal name della riga OdA, normalizzati a cifre."""
    out = []
    for m in re.findall(r'\[([^\]]+)\]', name or ''):
        d = only_digits(m)
        if d:
            out.append(d)
    return out


def norm_desc(s):
    s = (s or '').lower()
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def subset_sums_to(values, target, tol):
    """True se un sottoinsieme (k<=4) dei valori somma a target entro tol."""
    n = len(values)
    if n == 0:
        return False
    # singole
    for v in values:
        if abs(v - target) <= tol:
            return True
    # combinazioni fino a 4 elementi (e complemento)
    maxk = min(4, n)
    for k in range(2, maxk + 1):
        for combo in combinations(values, k):
            if abs(sum(combo) - target) <= tol:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--company', type=int, default=1)
    ap.add_argument('--months', type=int, default=2, help='eta max OdA in mesi')
    args = ap.parse_args()

    client = OdooReadOnlyClient(
        os.environ['ODOO_URL'], os.environ['ODOO_DB'],
        os.environ['ODOO_USERNAME'], os.environ['ODOO_PASSWORD'],
    )
    client.connect()

    attachments = client.get_fatturapa_attachments(
        only_unregistered=True, exclude_self_invoice=True,
        company_id=args.company,
    )
    print(f"Pending totali company {args.company}: {len(attachments)}")

    matcher = InvoiceMatcher(
        TOLLERANZA_PERCENTUALE, TOLLERANZA_ASSOLUTA,
        TOLLERANZA_TOTALE_FATTURA, classify_line_by_keyword,
    )
    analyzer = FatturaPAAnalyzer(
        client, matcher, TOLLERANZA_TOTALE_FATTURA,
        implicit_match_enabled=MATCH_IMPLICITO_ATTIVO,
        implicit_match_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_PERCENT,
        implicit_match_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA,
        implicit_match_loose_tolerance_absolute=TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA,
        implicit_match_loose_tolerance_percent=TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT,
        implicit_match_duplicate_guard=MATCH_IMPLICITO_GUARDIA_DUPLICATI,
        partial_match_enabled=MATCH_PARZIALE_ATTIVO,
        partial_match_max_rows=MATCH_PARZIALE_MAX_RIGHE,
        partial_match_max_extra_percent=MATCH_PARZIALE_MAX_EXTRA_PERCENT,
        partial_match_tolerance_absolute=MATCH_PARZIALE_TOLLERANZA_ASSOLUTA,
        suggestions_enabled=SUGGERIMENTI_ATTIVI,
        suggestions_max_lines=SUGGERIMENTI_MAX_RIGHE,
        suggestions_tolerance_absolute=SUGGERIMENTI_TOLLERANZA_ASSOLUTA,
        suggestions_max_age_months=SUGGERIMENTI_MAX_AGE_MONTHS,
        supplier_mapping_enabled=MAPPATURA_FORNITORI_FISSI_ATTIVA,
        supplier_mapping=MAPPATURA_FORNITORI_FISSI,
    )

    analyses = []
    for att in attachments:
        try:
            analyses.append(analyzer.analyze(att))
        except Exception:
            pass
    analyzer.apply_duplicate_guard(analyses)
    analyzer.apply_strict_wins_over_loose(analyses)
    analyzer.apply_run_cumulative_check(analyses)

    targets = [a for a in analyses if a.classification == TARGET_CLASS]
    print(f"Fatture {TARGET_CLASS}: {len(targets)}")
    print(f"Finestra OdA: ultimi {args.months} mesi (date_order)\n")

    tol = SUGGERIMENTI_TOLLERANZA_ASSOLUTA

    for a in targets:
        xd = a.xml_data
        imp = float(getattr(xd, 'imponibile_totale', 0) or 0)
        partner_id = getattr(a, '_partner_id_odoo', None)
        commesse = list(getattr(xd, 'commessa_riferimenti', []) or [])

        print("=" * 120)
        print(f"[att {a.attachment_id}] {a.supplier_name}  |  ft {getattr(xd,'numero','?')} "
              f"del {a.invoice_date}  |  imponibile €{imp:,.2f}  tot €{a.invoice_total:,.2f}")
        if commesse:
            print(f"   commesse XML: {', '.join(commesse)}")

        # righe fattura
        inv_codes = []
        inv_descs = []
        print("   righe fattura:")
        for ln in (xd.righe or []):
            cod = ln.codice_articolo_valore or ''
            inv_codes.append(only_digits(cod))
            inv_descs.append(norm_desc(ln.descrizione))
            print(f"     - [{cod or '-'}] {(ln.descrizione or '')[:55]:55s} "
                  f"q{ln.quantita:g} €{ln.prezzo_totale:,.2f}")
        inv_codes = [c for c in inv_codes if c]

        if not partner_id:
            print("   !! partner_id Odoo assente, skip query OdA")
            continue

        # OdA aperti del fornitore ultimi N mesi (anche invoice_status 'no' per ampliare)
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=args.months * 30)).strftime('%Y-%m-%d')
        pos = client._call(
            'purchase.order', 'search_read',
            [('partner_id', '=', partner_id),
             ('state', 'in', ['purchase', 'done']),
             ('invoice_status', 'in', ['to invoice', 'no']),
             ('date_order', '>=', cutoff)],
            fields=['id', 'name', 'origin', 'date_order', 'invoice_status',
                    'amount_untaxed', 'order_line'],
            order='date_order desc',
        )
        print(f"   OdA aperti fornitore (ultimi {args.months} mesi): {len(pos)}")
        if not pos:
            print("     -> nessun OdA recente: candidato a registrazione MANUALE / nuovo OdA")
            print()
            continue

        ranked = []
        for po in pos:
            lines = client.get_purchase_order_lines(po.get('order_line', [])) or []
            # valori riga OdA "residuo" (price_subtotal delle righe non ancora fatturate)
            subtot_vals = []
            po_codes = []
            po_descs = []
            for pl in lines:
                ps = float(pl.get('price_subtotal', 0) or 0)
                qinv = float(pl.get('qty_invoiced', 0) or 0)
                if qinv == 0 and ps != 0:
                    subtot_vals.append(ps)
                po_codes.extend(codes_in_po_name(pl.get('name', '')))
                po_descs.append(norm_desc(pl.get('name', '')))

            # EVIDENZA importo
            amount_hit = subset_sums_to(subtot_vals, imp, tol) if imp > 0 else False
            amount_full = abs(float(po.get('amount_untaxed', 0) or 0) - imp) <= tol

            # EVIDENZA codice articolo
            code_hits = sum(1 for c in inv_codes if any(c == pc or c in pc or pc in c
                                                        for pc in po_codes)) if po_codes else 0
            code_quota = (code_hits / len(inv_codes)) if inv_codes else 0.0

            # EVIDENZA descrizione
            best_desc = 0.0
            desc_hits = 0
            for d in inv_descs:
                if not d:
                    continue
                m = max((SequenceMatcher(None, d, pd).ratio() for pd in po_descs), default=0.0)
                best_desc = max(best_desc, m)
                if m >= 0.65:
                    desc_hits += 1
            desc_quota = (desc_hits / max(1, len([d for d in inv_descs if d])))

            # EVIDENZA commessa
            origin = (po.get('origin') or '')
            comm_hit = any(c in origin for c in commesse) if commesse else False

            score = (3 if amount_hit or amount_full else 0) \
                + (2 if code_quota >= 0.5 else (1 if code_quota > 0 else 0)) \
                + (2 if desc_quota >= 0.5 else (1 if best_desc >= 0.65 else 0)) \
                + (2 if comm_hit else 0)

            ranked.append((score, po, amount_hit, amount_full, code_quota,
                           best_desc, desc_quota, comm_hit, origin))

        ranked.sort(key=lambda r: r[0], reverse=True)
        for (score, po, amount_hit, amount_full, code_quota, best_desc,
             desc_quota, comm_hit, origin) in ranked:
            flags = []
            if amount_full:
                flags.append("IMPORTO=TOT")
            elif amount_hit:
                flags.append("IMPORTO=subset")
            if code_quota > 0:
                flags.append(f"codArt {code_quota*100:.0f}%")
            if best_desc >= 0.65:
                flags.append(f"desc {best_desc*100:.0f}%/{desc_quota*100:.0f}%righe")
            if comm_hit:
                flags.append("COMMESSA✓")
            tag = "  ".join(flags) if flags else "nessuna evidenza"
            star = " <<<" if score >= 3 else ""
            print(f"     score{score:>2} {po['name']:<10} {po.get('date_order','')[:10]} "
                  f"€{float(po.get('amount_untaxed',0)):>9,.2f} [{po.get('invoice_status')}] "
                  f"orig='{(origin or '-')[:18]:18s}'  {tag}{star}")
        print()


if __name__ == '__main__':
    main()
