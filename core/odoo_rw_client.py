"""
Client Odoo con capacità di scrittura per automazione bozze fatture.

IMPORTANTE: questo client va usato SOLO per operazioni esplicitamente
autorizzate (es. creazione bozze account.move, update purchase.order.line
nell'ambito della mappatura fornitori fissi).

Il client base OdooReadOnlyClient rimane read-only per tutto il codice
di lettura (matcher, analyzer, suggerimenti). Solo odoo_writer.py deve
usare questa classe.

Whitelist esplicita dei metodi write permessi, per sicurezza:
- create: creazione nuovi record
- write: aggiornamento record esistenti
- unlink: cancellazione record (usata solo per rollback bozze draft)

Tutti gli altri metodi (action_post, button_*, ecc.) restano bloccati.
"""

import logging
from core.odoo_client import OdooReadOnlyClient

logger = logging.getLogger(__name__)


class OdooReadWriteClient(OdooReadOnlyClient):
    """
    Estende OdooReadOnlyClient aggiungendo metodi di scrittura controllati.

    Whitelist write: create, write, unlink.
    Il resto dei metodi (action_post, button_confirm, ecc.) rimane bloccato
    per evitare di pubblicare fatture accidentalmente o toccare workflow Odoo.
    """

    # Include tutti i metodi permessi (read-only + write)
    ALLOWED_METHODS = {
        # Read (ereditati)
        'search', 'search_read', 'read', 'search_count', 'fields_get',
        # Write autorizzati
        'create', 'write', 'unlink',
    }

    def __init__(self, url: str, db: str, username: str, password: str):
        super().__init__(url, db, username, password)
        logger.warning(
            "OdooReadWriteClient attivato: scrittura abilitata. "
            "Usare solo per bozze controllate."
        )
