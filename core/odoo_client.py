"""
Client Odoo 14 via XML-RPC.
IMPORTANTE: utilizza solo metodi di lettura (search_read, read).
Nessun metodo create/write/unlink è implementato per sicurezza.
"""

import xmlrpc.client
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class OdooReadOnlyClient:
    """Client Odoo in sola lettura. Nessuna scrittura permessa by design."""

    # Whitelist esplicita dei metodi permessi
    ALLOWED_METHODS = {'search', 'search_read', 'read', 'search_count', 'fields_get'}

    def __init__(self, url: str, db: str, username: str, password: str):
        self.url = url.rstrip('/')
        self.db = db
        self.username = username
        self.password = password
        self.uid = None
        self._common = None
        self._models = None

    def connect(self) -> int:
        """Autentica l'utente e ritorna uid."""
        self._common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
        version = self._common.version()
        logger.info(f"Connesso a Odoo {version.get('server_version', 'unknown')}")

        self.uid = self._common.authenticate(self.db, self.username, self.password, {})
        if not self.uid:
            raise RuntimeError("Autenticazione fallita. Verificare credenziali.")

        self._models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')
        logger.info(f"Autenticato come uid={self.uid}")
        return self.uid

    def _call(self, model: str, method: str, *args, **kwargs):
        """Esegue una chiamata con controllo metodi ammessi."""
        if method not in self.ALLOWED_METHODS:
            raise PermissionError(
                f"Metodo '{method}' non ammesso. "
                f"Questo agent opera in sola lettura. "
                f"Metodi permessi: {self.ALLOWED_METHODS}"
            )

        return self._models.execute_kw(
            self.db, self.uid, self.password,
            model, method, list(args), kwargs
        )

    # ------------------------------------------------------------
    # FATTURE FORNITORE
    # ------------------------------------------------------------

    def get_vendor_bills(self, date_from: str, date_to: str,
                         states: List[str] = None) -> List[Dict]:
        """Recupera fatture fornitore nel range date, negli stati specificati."""
        states = states or ['draft']
        domain = [
            ('move_type', '=', 'in_invoice'),
            ('state', 'in', states),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
        ]
        fields = [
            'id', 'name', 'ref', 'partner_id', 'invoice_date', 'date',
            'amount_untaxed', 'amount_tax', 'amount_total', 'state',
            'invoice_line_ids', 'invoice_origin', 'narration',
            'currency_id', 'company_id',
            # Campi fatturazione elettronica italiana (se presenti)
            'l10n_it_einvoice_id' if self._has_field('account.move', 'l10n_it_einvoice_id') else None,
        ]
        fields = [f for f in fields if f]

        return self._call('account.move', 'search_read', domain, fields=fields)

    def get_invoice_lines(self, line_ids: List[int]) -> List[Dict]:
        """Recupera dettaglio righe fattura."""
        if not line_ids:
            return []
        fields = [
            'id', 'name', 'product_id', 'quantity', 'price_unit',
            'price_subtotal', 'price_total', 'account_id',
            'purchase_line_id', 'discount', 'tax_ids',
        ]
        return self._call('account.move.line', 'read', line_ids, fields=fields)

    # ------------------------------------------------------------
    # ORDINI DI ACQUISTO
    # ------------------------------------------------------------

    def search_purchase_order_by_name(self, name: str) -> Optional[Dict]:
        """Cerca un OdA per nome/codice (es. P01234)."""
        domain = [('name', '=', name)]
        fields = [
            'id', 'name', 'partner_id', 'date_order', 'state',
            'amount_untaxed', 'amount_tax', 'amount_total',
            'order_line', 'invoice_status',
            # company_id e currency_id servono al writer per costruire
            # correttamente il move (multi-company): non ometterli.
            'company_id', 'currency_id',
        ]
        result = self._call('purchase.order', 'search_read', domain, fields=fields, limit=1)
        return result[0] if result else None

    def get_purchase_order_lines(self, line_ids: List[int]) -> List[Dict]:
        """Recupera dettaglio righe OdA."""
        if not line_ids:
            return []
        fields = [
            'id', 'name', 'product_id', 'product_qty', 'qty_received',
            'qty_invoiced', 'price_unit', 'price_subtotal', 'price_total',
            'taxes_id', 'order_id',
        ]
        return self._call('purchase.order.line', 'read', line_ids, fields=fields)

    # ------------------------------------------------------------
    # FATTURAZIONE ELETTRONICA ITALIANA
    # ------------------------------------------------------------

    def get_einvoice_xml(self, einvoice_id: int) -> Optional[str]:
        """Recupera XML della fattura elettronica se disponibile."""
        try:
            result = self._call('l10n_it.edi.attachment', 'read',
                                [einvoice_id], fields=['datas', 'name'])
            return result[0] if result else None
        except Exception as e:
            logger.warning(f"Impossibile recuperare XML einvoice {einvoice_id}: {e}")
            return None

    # ------------------------------------------------------------
    # UTILITY
    # ------------------------------------------------------------

    def _has_field(self, model: str, field: str) -> bool:
        """Verifica se un campo esiste nel modello (per gestire moduli opzionali)."""
        try:
            fields = self._call(model, 'fields_get', [], attributes=['string'])
            return field in fields
        except Exception:
            return False

    def get_partner(self, partner_id: int) -> Dict:
        """Recupera anagrafica fornitore."""
        fields = ['id', 'name', 'vat', 'ref', 'supplier_rank']
        result = self._call('res.partner', 'read', [partner_id], fields=fields)
        return result[0] if result else {}

    def get_account(self, account_id: int) -> Dict:
        """Recupera info conto contabile."""
        fields = ['id', 'code', 'name', 'user_type_id']
        result = self._call('account.account', 'read', [account_id], fields=fields)
        return result[0] if result else {}

    # ------------------------------------------------------------
    # FATTURAPA - ALLEGATI E-FATTURE IN INGRESSO (DA REGISTRARE)
    # ------------------------------------------------------------

    def get_fatturapa_attachments(self, only_unregistered: bool = True,
                                  exclude_self_invoice: bool = True,
                                  company_id: Optional[int] = None,
                                  date_from: str = None,
                                  date_to: str = None,
                                  limit: int = None) -> List[Dict]:
        """
        Recupera gli allegati e-fattura in ingresso "da registrare".
        Replica il comportamento del filtro nativo di Odoo:
          registered=False AND is_self_invoice=False AND company_id=<scelta>

        Parametri:
        - only_unregistered=True: solo registered=False
        - exclude_self_invoice=True: esclude autofatture (reverse charge etc.)
        - company_id: filtra per azienda specifica (None = tutte)
        - date_from/date_to: range opzionale su create_date
        - limit: numero massimo di record

        Ritorna lista di dict con i campi rilevanti per l'analisi.
        """
        domain = []
        if only_unregistered:
            domain.append(('registered', '=', False))
        if exclude_self_invoice:
            domain.append(('is_self_invoice', '=', False))
        if company_id:
            domain.append(('company_id', '=', company_id))
        if date_from:
            domain.append(('create_date', '>=', date_from))
        if date_to:
            domain.append(('create_date', '<=', date_to + ' 23:59:59'))

        fields = [
            'id', 'name', 'att_name', 'xml_supplier_id',
            'invoices_total', 'invoices_date', 'invoices_number',
            'registered', 'in_invoice_ids', 'is_self_invoice',
            'inconsistencies', 'e_invoice_parsing_error',
            'e_invoice_validation_error', 'e_invoice_validation_message',
            'create_date', 'datas', 'company_id',
        ]

        kwargs = {'fields': fields, 'order': 'create_date desc'}
        if limit:
            kwargs['limit'] = limit

        return self._call('fatturapa.attachment.in', 'search_read', domain, **kwargs)

    def get_fatturapa_attachment_xml(self, attachment_id: int) -> Optional[str]:
        """Ritorna il contenuto XML (base64) di un allegato specifico."""
        result = self._call('fatturapa.attachment.in', 'read',
                            [attachment_id], fields=['datas'])
        if result and result[0].get('datas'):
            return result[0]['datas']
        return None

    # ------------------------------------------------------------
    # RICERCA MATCH IMPLICITO OdA (fornitore + importo)
    # ------------------------------------------------------------

    def search_po_by_partner_and_amount(self, partner_id: int,
                                         target_untaxed: float,
                                         tolerance_percent: float = 0.0,
                                         tolerance_absolute: float = 0.01,
                                         states: list = None,
                                         invoice_statuses: list = None) -> List[Dict]:
        """
        Cerca OdA per fornitore + importo entro tolleranza.
        Usato per il match implicito quando la fattura non cita l'OdA nell'XML.

        Parametri:
        - partner_id: ID del fornitore
        - target_untaxed: imponibile target (da confrontare con amount_untaxed)
        - tolerance_percent: tolleranza percentuale (default 0%)
        - tolerance_absolute: tolleranza assoluta in € (default 0.01)
        - states: stati OdA ammessi (default ['purchase'])
        - invoice_statuses: status fatturazione ammessi (default ['to invoice'])

        La tolleranza effettiva è il MASSIMO tra:
          - tolerance_absolute
          - target * tolerance_percent / 100

        Ritorna lista di OdA candidati ordinati per data. Se lunga 1 = match
        certo. Se lunga 2+ = match ambiguo, scelta manuale.
        """
        if not partner_id or not target_untaxed:
            return []

        states = states or ['purchase']
        invoice_statuses = invoice_statuses or ['to invoice']

        # Calcolo delta come massimo tra tolleranza assoluta e percentuale
        delta_percent = abs(target_untaxed) * (tolerance_percent / 100.0)
        delta = max(tolerance_absolute, delta_percent)
        min_amount = target_untaxed - delta
        max_amount = target_untaxed + delta

        domain = [
            ('partner_id', '=', partner_id),
            ('state', 'in', states),
            ('invoice_status', 'in', invoice_statuses),
            ('amount_untaxed', '>=', min_amount),
            ('amount_untaxed', '<=', max_amount),
        ]

        fields = [
            'id', 'name', 'partner_id', 'state', 'invoice_status',
            'amount_untaxed', 'amount_tax', 'amount_total',
            'date_order', 'order_line',
            'company_id', 'currency_id',
        ]

        return self._call('purchase.order', 'search_read', domain,
                         fields=fields, order='date_order desc')

    def get_all_open_pos_for_partner(self, partner_id: int,
                                      states: list = None,
                                      invoice_statuses: list = None,
                                      max_age_months: int = None) -> List[Dict]:
        """
        Recupera tutti gli OdA aperti (non fatturati) di un fornitore.
        Usato per il match parziale: si provano sottoinsiemi di righe
        fattura contro ciascun OdA candidato.

        max_age_months: se fornito, filtra OdA con date_order entro N mesi.
        """
        if not partner_id:
            return []
        states = states or ['purchase']
        invoice_statuses = invoice_statuses or ['to invoice', 'no']

        domain = [
            ('partner_id', '=', partner_id),
            ('state', 'in', states),
            ('invoice_status', 'in', invoice_statuses),
        ]

        if max_age_months:
            from datetime import datetime, timedelta
            cutoff_date = (datetime.now() - timedelta(days=max_age_months * 30)
                          ).strftime('%Y-%m-%d')
            domain.append(('date_order', '>=', cutoff_date))

        fields = [
            'id', 'name', 'partner_id', 'state', 'invoice_status',
            'amount_untaxed', 'amount_tax', 'amount_total',
            'date_order', 'order_line',
            # Necessari al writer (multi-company): senza questi
            # cade il filtro sul journal e si pesca uno di altra company.
            'company_id', 'currency_id',
        ]
        return self._call('purchase.order', 'search_read', domain,
                         fields=fields, order='date_order desc')

    # ------------------------------------------------------------
    # CUMULATO FATTURATO SU OdA
    # ------------------------------------------------------------

    def get_invoiced_amount_for_po(self, po_id: int,
                                   po_name: str = None) -> Dict:
        """
        Calcola il fatturato cumulativo per un ordine di acquisto.
        Considera sia le fatture già registrate (account.move posted/draft)
        sia le fatture potenzialmente già presenti su altri attachment
        non ancora registrati che referenziano lo stesso OdA.

        Ritorna dict con:
          - po_untaxed: imponibile OdA
          - already_invoiced_posted: somma fatture GIA' registrate (imponibile)
          - already_invoiced_draft: somma fatture in bozza (imponibile)
          - count_invoices: numero fatture collegate
          - invoices_info: lista di dict [{name, date, amount}]
        """
        result = {
            'po_untaxed': 0.0,
            'already_invoiced_posted': 0.0,
            'already_invoiced_draft': 0.0,
            'count_invoices': 0,
            'invoices_info': [],
        }

        # 1. Fatture in account.move collegate all'OdA
        # Uso il campo invoice_origin che contiene il nome OdA
        if po_name:
            try:
                bills = self._call(
                    'account.move', 'search_read',
                    [('move_type', '=', 'in_invoice'),
                     ('state', 'in', ['draft', 'posted']),
                     ('invoice_origin', '=', po_name)],
                    fields=['id', 'name', 'state', 'invoice_date',
                            'amount_untaxed', 'amount_total']
                )
                for b in bills:
                    amt = float(b.get('amount_untaxed', 0) or 0)
                    if b.get('state') == 'posted':
                        result['already_invoiced_posted'] += amt
                    else:
                        result['already_invoiced_draft'] += amt
                    result['count_invoices'] += 1
                    result['invoices_info'].append({
                        'name': b.get('name', ''),
                        'date': b.get('invoice_date', ''),
                        'amount': amt,
                        'state': b.get('state', ''),
                        'source': 'account.move',
                    })
            except Exception:
                pass

        return result
