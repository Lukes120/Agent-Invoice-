"""
Classificazione finale delle fatture in base al risultato del matching.
Categorie: AUTO_VALIDABILE, TRASPORTO_OK, DA_VERIFICARE, ODA_MANCANTE, ANOMALIA
"""

from datetime import datetime, date
from typing import List
from core.matcher import InvoiceAnalysis, LineMatch
from config.rules import (
    TOLLERANZA_TOTALE_FATTURA,
    PRIORITY_WEIGHTS,
    FORNITORI_CRITICI,
)


class InvoiceClassifier:
    """Assegna la categoria finale e calcola priority score."""

    def classify(self, analysis: InvoiceAnalysis) -> InvoiceAnalysis:
        """Applica la logica di classificazione all'analisi."""

        # Caso 1: OdA mancante
        if analysis.purchase_order is None:
            # Uso solo invoice_origin come indicatore di OdA atteso.
            # NB: 'ref' è il numero fattura del fornitore, non un OdA.
            oda_ref = analysis.invoice.get('invoice_origin')
            if oda_ref:
                analysis.classification = "ODA_MANCANTE"
                analysis.actions_suggested.append(
                    f"Riferimento OdA '{oda_ref}' citato in fattura ma non trovato in Odoo. "
                    f"Verificare se l'ordine esiste o richiedere correzione al fornitore."
                )
            else:
                # Nessun riferimento OdA: potrebbe essere fattura senza ordine (es. utenze)
                # Se TUTTE le righe sono riconosciute come keyword -> è uno scenario OK
                all_keyword = all(
                    lm.match_type == "KEYWORD" for lm in analysis.line_matches
                )
                if all_keyword and analysis.line_matches:
                    analysis.classification = "TRASPORTO_OK"
                    analysis.actions_suggested.append(
                        "Fattura senza OdA, tutte le righe classificate come spese accessorie. "
                        "Registrare sui conti suggeriti."
                    )
                else:
                    analysis.classification = "ODA_MANCANTE"
                    analysis.actions_suggested.append(
                        "Fattura senza riferimento OdA. "
                        "Verificare se l'ordine è mancante o se trattasi di fattura "
                        "non collegata a ordine (utenze, servizi)."
                    )
            analysis.priority_score = self._calc_priority(analysis)
            return analysis

        # Caso 2: OdA presente, analizzo i match delle righe
        exact_count = sum(1 for lm in analysis.line_matches if lm.match_type == "EXACT")
        tolerance_count = sum(1 for lm in analysis.line_matches if lm.match_type == "TOLERANCE")
        keyword_count = sum(1 for lm in analysis.line_matches if lm.match_type == "KEYWORD")
        out_of_tol_count = sum(1 for lm in analysis.line_matches if lm.match_type == "OUT_OF_TOLERANCE")
        no_match_count = sum(1 for lm in analysis.line_matches if lm.match_type == "NO_MATCH")

        total_lines = len(analysis.line_matches)
        matched_ok = exact_count + tolerance_count + keyword_count

        # Calcolo diff totale fattura vs totale OdA, escludendo le righe keyword
        # (trasporto, bolli, etc.) che sono legittimamente extra rispetto all'OdA
        keyword_amount = sum(
            float(lm.invoice_line.get('price_subtotal', 0))
            for lm in analysis.line_matches if lm.match_type == "KEYWORD"
        )
        inv_total = float(analysis.invoice.get('amount_untaxed', 0)) - keyword_amount
        po_total = float(analysis.purchase_order.get('amount_untaxed', 0))
        analysis.total_diff = inv_total - po_total
        if po_total:
            analysis.total_diff_percent = abs(analysis.total_diff / po_total * 100)

        within_total_tol = abs(analysis.total_diff) <= TOLLERANZA_TOTALE_FATTURA

        # Decisione finale
        if no_match_count > 0:
            analysis.classification = "DA_VERIFICARE"
            analysis.actions_suggested.append(
                f"{no_match_count} righe fattura non riconosciute. "
                f"Verificare manualmente la corrispondenza con l'OdA."
            )
        elif out_of_tol_count > 0:
            analysis.classification = "DA_VERIFICARE"
            analysis.actions_suggested.append(
                f"{out_of_tol_count} righe con scostamento oltre tolleranza. "
                f"Verificare prezzi e quantità con il fornitore."
            )
        elif matched_ok == total_lines and within_total_tol:
            if keyword_count > 0 and (exact_count + tolerance_count) > 0:
                # Fattura mista: merce OK + righe di spese extra riconosciute
                analysis.classification = "AUTO_VALIDABILE"
                analysis.actions_suggested.append(
                    f"Fattura OK: {exact_count + tolerance_count} righe merce matchate, "
                    f"{keyword_count} righe spese accessorie riconosciute. Registrare."
                )
            elif keyword_count == total_lines:
                analysis.classification = "TRASPORTO_OK"
                analysis.actions_suggested.append(
                    "Tutte le righe sono spese accessorie riconosciute. "
                    "Registrare sui conti suggeriti."
                )
            else:
                analysis.classification = "AUTO_VALIDABILE"
                analysis.actions_suggested.append(
                    "Tutte le righe matchate con OdA entro tolleranza. Registrare."
                )
        else:
            # Totale fattura fuori tolleranza anche se le singole righe tornano
            analysis.classification = "DA_VERIFICARE"
            analysis.actions_suggested.append(
                f"Totale fattura scostato di €{analysis.total_diff:.2f} "
                f"({analysis.total_diff_percent:.1f}%) dal totale OdA. "
                f"Verificare presenza di righe non matchate o differenze cumulate."
            )

        analysis.priority_score = self._calc_priority(analysis)
        return analysis

    def _calc_priority(self, analysis: InvoiceAnalysis) -> float:
        """
        Calcola score di priorità per ordinare la lista eccezioni.
        Score più alto = più urgente.
        """
        if analysis.classification == "AUTO_VALIDABILE":
            return 0.0
        if analysis.classification == "TRASPORTO_OK":
            return 0.0

        score = 0.0

        # Peso importo (normalizzato su 10k€)
        importo = abs(float(analysis.invoice.get('amount_total', 0)))
        score += PRIORITY_WEIGHTS["importo"] * min(importo / 10000.0, 1.0)

        # Peso anzianità fattura
        inv_date_str = analysis.invoice.get('invoice_date')
        if inv_date_str:
            try:
                inv_date = datetime.strptime(inv_date_str, '%Y-%m-%d').date()
                days_old = (date.today() - inv_date).days
                score += PRIORITY_WEIGHTS["anzianita"] * min(days_old / 60.0, 1.0)
            except Exception:
                pass

        # Peso fornitore critico
        partner = analysis.invoice.get('partner_id')
        if partner and FORNITORI_CRITICI:
            partner_name = partner[1] if isinstance(partner, list) else str(partner)
            if any(crit in partner_name for crit in FORNITORI_CRITICI):
                score += PRIORITY_WEIGHTS["fornitore"]

        # Peso scostamento
        if analysis.total_diff_percent:
            score += PRIORITY_WEIGHTS["scostamento"] * min(
                analysis.total_diff_percent / 20.0, 1.0
            )

        return round(score, 3)
