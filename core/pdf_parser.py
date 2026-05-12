"""
Parser PDF per fatture del network Telepass (Autostrade in primis).

Scopo principale: estrarre la lista APPARATO/TESSERA con il totale dei pedaggi
per consentire lo split automatico Furgoni (420160 100%) / Uso Promiscuo
(420840 70%) sulle fatture Autostrade. Senza questa info, lo split richiede
intervento manuale del contabile (R1 attuale). Con questa info + la mappa
apparati Ecotel si arriva al 95-100% di automazione (R4).

Layout atteso (verificato su 9+ PDF Autostrade Q1 2026):
- Pagina 1: header con totale fattura, codice cliente, IVA breakdown
- Pagine 2..N-1: per ogni apparato sezione con
    "APPARATO TELEPASS <num>" o "TESSERA VIACARD <num>"
    riga per ogni viaggio: data ora SRV "PED tratta" CLASSE importo_iva_incl
    riga finale: "Totale numero movimenti N IMPORTO X,XX"
- Pagina N: riepilogo CLASSE A / B con importi totali

Gli importi nella sezione apparato sono IVA INCLUSA. Per ricavare l'imponibile
per categoria si usa la PROPORZIONE (IVA-inclusa per apparato / IVA-inclusa
totale) applicata all'imponibile XML — più robusto della divisione per 1,22
quando ci possono essere IVA miste.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None


# Pattern di estrazione (precompilati)

# "APPARATO TELEPASS 286352315" o "APPARATO TELEPASS  286352315" (vari spazi)
_APPARATO_TLP = re.compile(
    r'\bAPPARATO\s+TELEPASS\s+(\d{6,})',
    re.IGNORECASE
)
# "TESSERA VIACARD 385592119" (cifre) o "TESSERA VIACARD 3.855592.19" (puntato)
_TESSERA_VIACARD = re.compile(
    r'\bTESSERA\s+VIACARD\s+(\d+(?:\.\d+){0,2})',
    re.IGNORECASE
)
# "Totale numero movimenti 12 IMPORTO 72,70"  (Autostrade)
# "NUMERO SOSTE PARCHEGGI 12 IMPORTO 132,00"  (Apcoa)
# Tollera: spazi multipli, importo con punto migliaia ("1.234,56") o senza
_TOTALE_MOVIMENTI = re.compile(
    r'(?:Totale\s+numero\s+movimenti|NUMERO\s+SOSTE\s+PARCHEGGI)'
    r'\s+(\d+)\s+IMPORTO\s+(\d+(?:\.\d{3})*,\d{2})',
    re.IGNORECASE
)
# "TOTALE € 763,00" o "TOTALE 763,00" (può avere il simbolo euro)
_TOTALE_FATTURA = re.compile(
    r'TOTALE\s*[€€]?\s*(\d+(?:\.\d{3})*,\d{2})',
)


@dataclass
class ApparatoTotale:
    """Totale pedaggi per un singolo apparato (IVA inclusa)."""
    tipo: str  # 'TELEPASS' | 'VIACARD'
    apparato_id: str
    n_movimenti: int
    importo_iva_inclusa: float

    @property
    def chiave_lookup(self) -> str:
        """Chiave per lookup nella APPARATI_MAP (normalizzata).

        Per VIACARD format puntato resta tale; per cifre semplici resta tale.
        Allineato con scripts/generate_apparati_mapping.py.
        """
        return self.apparato_id


@dataclass
class FatturaPdfData:
    """Dati estratti dal PDF di una fattura Autostrade-style."""
    apparati: List[ApparatoTotale] = field(default_factory=list)
    totale_fattura_iva_inclusa: Optional[float] = None
    parsing_errors: List[str] = field(default_factory=list)

    @property
    def somma_importi_apparati(self) -> float:
        return round(sum(a.importo_iva_inclusa for a in self.apparati), 2)


def _it_to_float(s: str) -> float:
    """Converte '1.234,56' o '72,70' in float."""
    return float(s.replace('.', '').replace(',', '.'))


def parse_pdf_autostrade(pdf_path: str) -> FatturaPdfData:
    """
    Estrae la lista (APPARATO/TESSERA, totale_iva_incl) dal PDF Autostrade.

    Robusto a:
    - apparati che si estendono su più pagine (aggrega per id)
    - "Totale numero movimenti" che può comparire prima del prossimo header
    - layout multi-colonna (risolto da pdfplumber.extract_text con default
      x_tolerance/y_tolerance)
    """
    if pdfplumber is None:
        raise RuntimeError("pdfplumber non disponibile (pip install pdfplumber)")

    result = FatturaPdfData()
    if not pdf_path:
        result.parsing_errors.append("pdf_path vuoto")
        return result

    full_text_lines: List[str] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                full_text_lines.extend(text.split("\n"))
    except Exception as e:
        result.parsing_errors.append(f"PDF open/read error: {e}")
        return result

    # Cerca totale fattura nella pagina iniziale
    for ln in full_text_lines[:80]:
        m = _TOTALE_FATTURA.search(ln)
        if m and "FATTURA" in ln.upper():
            try:
                result.totale_fattura_iva_inclusa = _it_to_float(m.group(1))
            except Exception:
                pass
            break

    # Scansione: per ogni riga, individuo header apparato/tessera oppure
    # totale movimenti e li abbino. L'idea: stato "current" che ricorda
    # l'ultimo apparato visto; quando trovo il "Totale numero movimenti"
    # gli associo n_movimenti+importo.
    current_tipo: Optional[str] = None
    current_id: Optional[str] = None
    seen_ids: Dict[Tuple[str, str], ApparatoTotale] = {}

    for ln in full_text_lines:
        m_tlp = _APPARATO_TLP.search(ln)
        m_via = _TESSERA_VIACARD.search(ln)
        m_tot = _TOTALE_MOVIMENTI.search(ln)

        if m_tlp:
            current_tipo = "TELEPASS"
            current_id = m_tlp.group(1)
        elif m_via:
            current_tipo = "VIACARD"
            current_id = m_via.group(1)
        elif m_tot and current_tipo and current_id:
            try:
                n_mov = int(m_tot.group(1))
                importo = _it_to_float(m_tot.group(2))
            except Exception as e:
                result.parsing_errors.append(
                    f"parse totale fallito su '{ln.strip()[:80]}': {e}")
                continue
            key = (current_tipo, current_id)
            if key in seen_ids:
                # Stesso apparato comparso più volte (raro ma possibile per
                # totali parziali multipagina). Tengo il PRIMO incontrato e
                # segnalo a log.
                result.parsing_errors.append(
                    f"Apparato {current_tipo} {current_id} "
                    f"con doppio totale: ignoro {importo} (tengo "
                    f"{seen_ids[key].importo_iva_inclusa})")
                continue
            seen_ids[key] = ApparatoTotale(
                tipo=current_tipo,
                apparato_id=current_id,
                n_movimenti=n_mov,
                importo_iva_inclusa=importo,
            )
            current_tipo = None
            current_id = None

    result.apparati = list(seen_ids.values())

    if not result.apparati:
        result.parsing_errors.append(
            "Nessun apparato trovato — il PDF non ha layout Autostrade?")

    return result


def calcola_split_furgoni_promiscuo(
        pdf_data: FatturaPdfData,
        imponibile_xml: float,
        get_classificazione_func) -> Dict:
    """
    Calcola lo split imponibile_xml in 2 quote (furgoni / uso_promiscuo)
    usando la mappatura apparati e la proporzione dei totali IVA-inclusa.

    Args:
        pdf_data: output di parse_pdf_autostrade
        imponibile_xml: imponibile fattura (senza IVA) preso dall'XML FatturaPA
        get_classificazione_func: callable apparato_id -> 'furgoni' |
                                   'uso_promiscuo' | None.
                                   Tipicamente
                                   `from config.apparati_mapping import get_classificazione`.

    Returns:
        dict con:
            'imponibile_furgoni': float
            'imponibile_promiscuo': float
            'apparati_furgoni': list[ApparatoTotale]
            'apparati_promiscuo': list[ApparatoTotale]
            'apparati_non_mappati': list[ApparatoTotale]
            'totale_iva_inclusa_pdf': float (somma totali apparati)
            'warnings': list[str]
    """
    warnings: List[str] = []

    if not pdf_data.apparati:
        return {
            'imponibile_furgoni': 0.0,
            'imponibile_promiscuo': 0.0,
            'apparati_furgoni': [],
            'apparati_promiscuo': [],
            'apparati_non_mappati': [],
            'totale_iva_inclusa_pdf': 0.0,
            'warnings': ['PDF senza apparati estratti — impossibile fare split'],
        }

    apparati_furgoni: List[ApparatoTotale] = []
    apparati_promiscuo: List[ApparatoTotale] = []
    apparati_non_mappati: List[ApparatoTotale] = []

    for app in pdf_data.apparati:
        cls = get_classificazione_func(app.apparato_id)
        if cls == 'furgoni':
            apparati_furgoni.append(app)
        elif cls == 'uso_promiscuo':
            apparati_promiscuo.append(app)
        else:
            apparati_non_mappati.append(app)

    if apparati_non_mappati:
        details = ", ".join(
            f"{a.tipo} {a.apparato_id} (€{a.importo_iva_inclusa:.2f})"
            for a in apparati_non_mappati[:5])
        warnings.append(
            f"{len(apparati_non_mappati)} apparati NON mappati "
            f"in APPARATI_MAP: {details}. Aggiornare PARCO AUTO.")

    tot_iva_inclusa = sum(a.importo_iva_inclusa for a in pdf_data.apparati)
    if tot_iva_inclusa <= 0:
        return {
            'imponibile_furgoni': 0.0,
            'imponibile_promiscuo': 0.0,
            'apparati_furgoni': apparati_furgoni,
            'apparati_promiscuo': apparati_promiscuo,
            'apparati_non_mappati': apparati_non_mappati,
            'totale_iva_inclusa_pdf': 0.0,
            'warnings': warnings + ['Totale IVA-inclusa = 0, no split possibile'],
        }

    # Calcolo proporzioni e applico all'imponibile_xml
    iva_furgoni = sum(a.importo_iva_inclusa for a in apparati_furgoni)
    iva_promiscuo = sum(a.importo_iva_inclusa for a in apparati_promiscuo)

    # Apparati non mappati: per default li metto come "uso_promiscuo"
    # (scelta conservativa: meno deducibilità). L'utente può cambiare a mano.
    iva_non_mappati = sum(a.importo_iva_inclusa for a in apparati_non_mappati)
    if iva_non_mappati > 0:
        warnings.append(
            f"€{iva_non_mappati:.2f} IVA-inclusa apparati non mappati "
            f"assegnati di default a 'uso_promiscuo'.")
        iva_promiscuo += iva_non_mappati

    proporzione_furgoni = iva_furgoni / tot_iva_inclusa
    imponibile_furgoni = round(imponibile_xml * proporzione_furgoni, 2)
    imponibile_promiscuo = round(imponibile_xml - imponibile_furgoni, 2)

    return {
        'imponibile_furgoni': imponibile_furgoni,
        'imponibile_promiscuo': imponibile_promiscuo,
        'apparati_furgoni': apparati_furgoni,
        'apparati_promiscuo': apparati_promiscuo,
        'apparati_non_mappati': apparati_non_mappati,
        'totale_iva_inclusa_pdf': round(tot_iva_inclusa, 2),
        'warnings': warnings,
    }
