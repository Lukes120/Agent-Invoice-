"""
Mappa carte carburante Edenred UTA Mobility -> classificazione fiscale veicolo (Ecotel).

DINAMICA: caricata da input/carte_uta.xlsx con auto-refresh su mtime.
Aggiungere/rimuovere/modificare righe nell'XLSX -> al prossimo accesso le
carte vengono ricaricate automaticamente (no rigenerazione richiesta).

Schema XLSX (foglio "CARTE UTA" o "Foglio1"):
    numero_carta | targa | nickname | classificazione_fiscale | stato

Solo righe con stato='ATTIVO' vengono incluse.

Per validare manualmente l'XLSX (lint duplicati / classificazioni anomale):
    python scripts/generate_carte_carburante_mapping.py --print-summary
"""
from pathlib import Path
from typing import Optional

from config._carte_xlsx_loader import XlsxBackedCardMap

# Costanti classificazione
CLASSIFICAZIONE_POOL = "POOL"
CLASSIFICAZIONE_USO_PROMISCUO = "uso_promiscuo"
CLASSIFICAZIONE_SUPER_LUSSO = "super_lusso"
CLASSIFICAZIONE_SERVIZIO = "SERVIZIO"

_XLSX_PATH = Path(__file__).resolve().parent.parent / "input" / "carte_uta.xlsx"

# Mappa per NUMERO CARTA UTA -> info veicolo/classe
# Chiave: stringa numero_carta (RiferimentoAmministrazione XML)
# Auto-refresh: ricarica da XLSX se modificato.
CARTE_UTA_BY_NUMERO = XlsxBackedCardMap(_XLSX_PATH)


def get_carta_uta(numero: str) -> Optional[dict]:
    """Ritorna info carta UTA dato il numero (stringa, normalizzata)."""
    if numero is None:
        return None
    return CARTE_UTA_BY_NUMERO.get(str(numero).strip())


def get_classificazione_carta_uta(numero: str) -> Optional[str]:
    """Ritorna classificazione fiscale (POOL/uso_promiscuo/super_lusso/SERVIZIO)
    o None se carta non in mappa (riga da censire)."""
    info = get_carta_uta(numero)
    return info["classificazione"] if info else None
