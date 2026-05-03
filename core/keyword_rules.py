"""
Riconoscimento automatico righe fattura per categorie note
(trasporto, spedizione, bolli, imballaggi, etc.).

IMPORTANTE: le keyword devono catturare SOLO righe che sono chiaramente
spese accessorie (es. "Spese di trasporto", "Spedizione"), NON righe
che contengono la parola per caso (es. "Attività di SUPPORTO" o "IMPORTO").

Strategia: usiamo regex con word-boundary e contesto, non semplici sostringhe.
"""

import re
from typing import Optional, Tuple, List
from config.rules import KEYWORD_RULES, CONTI_CONTABILI


# Precompilazione pattern robusti per evitare falsi positivi.
# Ogni pattern cerca la parola come token separato, non come sostringa.
# Le keyword della config/rules.py vengono comunque rispettate come override,
# ma qui definiamo il comportamento "di default" più sicuro.

_PATTERN_CACHE = None

def _build_patterns():
    """Crea regex word-boundary per ogni keyword definita."""
    global _PATTERN_CACHE
    if _PATTERN_CACHE is not None:
        return _PATTERN_CACHE

    patterns = []
    for radice, conto_key, categoria in KEYWORD_RULES:
        # Costruisco una regex che cerchi la radice come inizio di parola intera.
        # Es: "trasport" matcha "trasporto/i/ate/azione" ma NON "supporto".
        # La lookahead \S* permette suffissi (trasportO, trasportATI, ecc.)
        pattern = re.compile(
            r'\b' + re.escape(radice) + r'[a-zA-Zàèéìòù]*\b',
            re.IGNORECASE
        )
        patterns.append((pattern, conto_key, categoria, radice))
    _PATTERN_CACHE = patterns
    return patterns


# Blacklist di parole che contengono le keyword ma NON sono spese accessorie
# Serve come doppio filtro di sicurezza
BLACKLIST_CONTESTI = [
    'supporto', 'supportare', 'supportiamo', 'importo', 'importi',
    'importazione', 'importare', 'apporto', 'apportare', 'rapporto',
    'trasportato', 'trasportatore',  # questi potrebbero essere ambigui
    'consegnato',  # può riferirsi al bene principale, non al trasporto
]


def classify_line_by_keyword(description: str) -> Optional[Tuple[str, str, str]]:
    """
    Classifica una riga fattura come spesa accessoria SE e SOLO SE:
    1. La descrizione contiene una delle keyword definite
    2. La keyword è una parola intera (word-boundary)
    3. La descrizione non contiene parole della blacklist che neutralizzano

    Ritorna (conto_key, conto_codice, categoria) oppure None.
    """
    if not description:
        return None

    desc_lower = description.lower()

    # Controllo blacklist PRIMA di controllare le keyword
    # Se la descrizione contiene parole "ingannevoli", non classifico
    for bad in BLACKLIST_CONTESTI:
        if bad in desc_lower:
            # Verifica se oltre alla parola blacklist c'è anche un vero indicatore.
            # Se non c'è, scarto.
            has_real_indicator = any(
                kw in desc_lower for kw in [
                    'spese di trasport', 'spesa trasport', 'costi di trasport',
                    'spese di spedizion', 'spese di consegn', 'spese di imball',
                    'contributo spedizion', 'porto franco', 'porto assegnato',
                ]
            )
            if not has_real_indicator:
                return None

    # Cerco match con pattern word-boundary
    for pattern, conto_key, categoria, radice in _build_patterns():
        if pattern.search(description):
            conto_codice = CONTI_CONTABILI.get(conto_key, "DA_COMPILARE")
            return (conto_key, conto_codice, categoria)

    return None


def extract_oda_references(text: str, patterns: list) -> list:
    """
    Estrae riferimenti OdA dal testo usando una lista di pattern regex.
    """
    if not text:
        return []

    found = []
    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        found.extend(matches)

    seen = set()
    unique = []
    for ref in found:
        if ref.upper() not in seen:
            seen.add(ref.upper())
            unique.append(ref.upper())

    return unique
