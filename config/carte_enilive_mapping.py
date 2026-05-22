"""
Mappa carte carburante Enilive -> classificazione fiscale veicolo (Ecotel).

DINAMICA: caricata da input/carte_enilive.xlsx con auto-refresh su mtime.
Aggiungere/rimuovere/modificare righe nell'XLSX -> al prossimo accesso le
carte vengono ricaricate automaticamente (no rigenerazione richiesta).

Schema XLSX (foglio "Foglio1" o "CARTE ENILIVE"):
    numero_carta | targa | nickname | classificazione_fiscale | stato

Solo righe con stato='ATTIVO' vengono incluse.

Per validare manualmente l'XLSX (lint duplicati / classificazioni anomale):
    python scripts/generate_carte_enilive_mapping.py --print-summary
"""
from pathlib import Path
from typing import Optional

from config._carte_xlsx_loader import XlsxBackedCardMap

# Costanti classificazione (allineate a carte_carburante_mapping/UTA)
CLASSIFICAZIONE_POOL = "POOL"
CLASSIFICAZIONE_USO_PROMISCUO = "uso_promiscuo"
CLASSIFICAZIONE_SUPER_LUSSO = "super_lusso"
CLASSIFICAZIONE_SERVIZIO = "SERVIZIO"

_XLSX_PATH = Path(__file__).resolve().parent.parent / "input" / "carte_enilive.xlsx"

# Mappa per NUMERO CARTA ENILIVE -> info veicolo/classe
# Auto-refresh: ricarica da XLSX se modificato.
CARTE_ENILIVE_BY_NUMERO = XlsxBackedCardMap(_XLSX_PATH)


def get_carta_enilive(numero: str) -> Optional[dict]:
    """Ritorna info carta Enilive dato il numero (stringa, normalizzata)."""
    if numero is None:
        return None
    return CARTE_ENILIVE_BY_NUMERO.get(str(numero).strip())


def get_classificazione_carta_enilive(numero: str) -> Optional[str]:
    """Ritorna classificazione fiscale (POOL/uso_promiscuo/super_lusso/SERVIZIO)
    o None se carta non in mappa (riga da censire)."""
    info = get_carta_enilive(numero)
    return info["classificazione"] if info else None
