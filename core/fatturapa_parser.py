"""
Parser XML FatturaPA (formato italiano standard).

Estrae dai blocchi XML:
- Dati fornitore (partita IVA, denominazione)
- Dati fattura (numero, data, totali)
- Riferimenti OdA (DatiOrdineAcquisto/IdDocumento)
- Riferimenti Contratto/Convenzione/Ricezione (fallback OdA)
- Causale
- Righe dettaglio (DettaglioLinee)

L'XML FatturaPA può avere prefissi namespace (es. ns2:) o non averli.
Il parser gestisce entrambi i casi.
"""

import re
import base64
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


# ============================================================
# PATTERN RICONOSCIMENTO OdA
# ============================================================
# Ordinati per specificità: il più specifico prima.
# Ogni pattern estrae con gruppo catturante il codice OdA normalizzato.

ODA_PATTERNS = [
    # P seguito da 5 cifre (formato principale Ecotel)
    (re.compile(r'\b(P\d{5})\b', re.IGNORECASE), 'P5'),
    # P seguito da 4 cifre (formato più vecchio o variante)
    (re.compile(r'\b(P\d{4})\b(?!\d)', re.IGNORECASE), 'P4'),
    # PO / PO- / PO/ seguito da cifre
    (re.compile(r'\b(PO[-/]?\d{3,7})\b', re.IGNORECASE), 'PO'),
    # ORD seguito da cifre
    (re.compile(r'\b(ORD[-/]?\d{3,7})\b', re.IGNORECASE), 'ORD'),
]

# Pattern commessa (S + 5 cifre) - IDENTIFICATO MA NON MATCHATO IN FASE 1
# L'agent lo segnala nei messaggi per uso futuro
COMMESSA_PATTERN = re.compile(r'\b(S\d{5})\b', re.IGNORECASE)


@dataclass
class FatturaPALine:
    """Singola riga di dettaglio fattura elettronica."""
    numero_linea: int = 0
    descrizione: str = ""
    quantita: float = 0.0
    unita_misura: str = ""
    prezzo_unitario: float = 0.0
    prezzo_totale: float = 0.0
    aliquota_iva: float = 0.0
    # Eventuale riferimento OdA a livello di riga
    riferimenti_oda: List[str] = field(default_factory=list)
    # Codice articolo strutturato dal nodo <CodiceArticolo> (FatturaPA std).
    # Esempio Wuerth: "005716 80 005" — combacia con il codice tra [...] nel
    # name della riga OdA Odoo. Usato dal match implicito multi-evidenza.
    codice_articolo_valore: str = ""
    codice_articolo_tipo: str = ""


@dataclass
class FatturaPAData:
    """Dati strutturati estratti da un XML FatturaPA."""
    # Fornitore
    cedente_partita_iva: str = ""
    cedente_codice_fiscale: str = ""
    cedente_denominazione: str = ""
    # Identificativo del rapporto col cessionario (Wind Tre lo usa per
    # distinguere il contratto/OdA quando manca DatiContratto)
    cedente_riferimento_amministrazione: str = ""

    # Documento
    tipo_documento: str = ""      # TD01 (fattura), TD04 (nota credito), ecc.
    numero: str = ""
    data: str = ""
    divisa: str = "EUR"

    # Totali
    importo_totale: float = 0.0
    imponibile_totale: float = 0.0
    imposta_totale: float = 0.0

    # Riferimenti
    oda_riferimenti: List[str] = field(default_factory=list)
    # Riferimenti "sporchi" originali (per debug/verifica)
    oda_valori_grezzi: List[str] = field(default_factory=list)
    # Riferimenti OdA trovati in descrizioni riga / causali (match testuale)
    # Separati da oda_riferimenti perché meno affidabili
    oda_riferimenti_testuali: List[str] = field(default_factory=list)
    # Altri riferimenti: contratto, convenzione, ricezione, commessa
    commessa_riferimenti: List[str] = field(default_factory=list)
    contratto_riferimenti: List[str] = field(default_factory=list)
    ricezione_riferimenti: List[str] = field(default_factory=list)
    # POD/PDR estratti dalle descrizioni linee (utility energia/gas).
    # Formato standard italiano: IT + 3 cifre + lettera + 8 cifre (es. IT001E68725584)
    pod_riferimenti: List[str] = field(default_factory=list)

    # Causale
    causali: List[str] = field(default_factory=list)

    # Righe
    righe: List[FatturaPALine] = field(default_factory=list)

    # Errori / warning di parsing
    parsing_errors: List[str] = field(default_factory=list)


def _strip_namespace(tag: str) -> str:
    """Rimuove il prefisso namespace {uri}tag -> tag."""
    if '}' in tag:
        return tag.split('}', 1)[1]
    return tag


def _find_all(root, path: List[str]):
    """
    Cerca elementi seguendo un path di tag, ignorando namespace.
    path = ['FatturaElettronicaBody', 'DatiGenerali', 'DatiOrdineAcquisto']
    """
    results = [root]
    for tag in path:
        new_results = []
        for node in results:
            for child in node:
                if _strip_namespace(child.tag) == tag:
                    new_results.append(child)
        results = new_results
    return results


def _find_first(root, path: List[str]):
    """Come _find_all ma ritorna solo il primo match."""
    results = _find_all(root, path)
    return results[0] if results else None


def _get_text(node, tag: str) -> str:
    """Ritorna il testo del primo figlio con tag specificato."""
    if node is None:
        return ""
    for child in node:
        if _strip_namespace(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def _safe_float(value: str) -> float:
    """Converte stringa in float gestendo virgole decimali italiane."""
    if not value:
        return 0.0
    try:
        return float(value.replace(',', '.'))
    except (ValueError, AttributeError):
        return 0.0


def _normalize_oda(raw_value: str) -> List[str]:
    """
    Normalizza un valore grezzo estratto da <IdDocumento>.
    Estrae tutti i pattern OdA riconoscibili.
    Esempi:
      'P04368 - 26.03.2026' -> ['P04368']
      'comm: S03146 ATM SAN' -> []  (è una commessa, non un OdA)
      'Off.479/RM' -> []            (è un'offerta, non un OdA)
      'P04368, P04369' -> ['P04368', 'P04369']
    """
    if not raw_value:
        return []

    found = []
    for pattern, _label in ODA_PATTERNS:
        matches = pattern.findall(raw_value)
        for m in matches:
            normalized = m.upper().replace('-', '').replace('/', '').replace(' ', '')
            if normalized not in found:
                found.append(normalized)
    return found


def _extract_commessa_refs(text: str) -> List[str]:
    """Estrae riferimenti a commesse S##### dal testo."""
    if not text:
        return []
    matches = COMMESSA_PATTERN.findall(text)
    return list(dict.fromkeys(m.upper() for m in matches))


# Parole-chiave che indicano contesto di "ordine d'acquisto" nel testo libero.
# Se una di queste appare nel testo, consideriamo i pattern OdA vicini come
# veri OdA (non codici prodotto casuali).
_ORDER_CONTEXT_WORDS = re.compile(
    r'\b(?:ordine|ordin[aei]|ordre|'
    r'ord\.?\s*n|ns\.?\s*ord|vs\.?\s*ord|'
    r'ns\.?\s*oda|vs\.?\s*oda|rif\.?\s*ord|'
    r'oda|odi|acqu\w+|acquisto|commessa\s+aperta)\b',
    re.IGNORECASE
)

# Pattern OdA (P + 4-5 cifre) usato nell'estrazione testuale
_ODA_IN_TEXT = re.compile(r'\b(P\d{4,5})\b', re.IGNORECASE)

# Pattern POD/PDR italiano (utility energia/gas).
# Formato: IT + 3 cifre + 1 lettera + 8 cifre (es. IT001E68725584).
# Per il gas può essere 14 cifre senza lettera, ma copriamo principalmente energia.
_POD_PATTERN = re.compile(r'\b(IT\d{3}[A-Z]\d{8})\b')


def _extract_pods(text: str) -> List[str]:
    if not text:
        return []
    return list(dict.fromkeys(_POD_PATTERN.findall(text)))


def _extract_oda_from_text(text: str) -> List[str]:
    """
    Estrae riferimenti OdA dal testo libero (descrizioni riga, causali).
    PRUDENTE: richiede parole-chiave di contesto (ordine, ODA, ecc) per
    ridurre falsi positivi da codici prodotto casuali.

    Strategia: se il testo contiene almeno una parola-chiave "ordine/oda/acqu*",
    allora raccoglie tutti i pattern P##### presenti. Altrimenti, skip.
    """
    if not text:
        return []

    # Serve una parola-chiave di contesto da qualche parte nel testo
    if not _ORDER_CONTEXT_WORDS.search(text):
        return []

    found = []
    for match in _ODA_IN_TEXT.finditer(text):
        code = match.group(1).upper()
        if code not in found:
            found.append(code)
    return found


def parse_fatturapa_xml(xml_content: str) -> FatturaPAData:
    """
    Parsa un XML FatturaPA e ritorna la struttura dati.
    xml_content: stringa XML (non base64).
    """
    data = FatturaPAData()

    try:
        # Rimuovo BOM se presente
        if xml_content.startswith('\ufeff'):
            xml_content = xml_content[1:]

        root = ET.fromstring(xml_content)

        # === HEADER: FatturaElettronicaHeader/CedentePrestatore ===
        cedente = _find_first(root, ['FatturaElettronicaHeader', 'CedentePrestatore'])
        if cedente is not None:
            dati_anag = _find_first(cedente, ['DatiAnagrafici'])
            if dati_anag is not None:
                # Partita IVA
                id_fiscale = _find_first(dati_anag, ['IdFiscaleIVA'])
                if id_fiscale is not None:
                    paese = _get_text(id_fiscale, 'IdPaese')
                    codice = _get_text(id_fiscale, 'IdCodice')
                    data.cedente_partita_iva = f"{paese}{codice}"

                data.cedente_codice_fiscale = _get_text(dati_anag, 'CodiceFiscale')

                # Denominazione
                anagrafica = _find_first(dati_anag, ['Anagrafica'])
                if anagrafica is not None:
                    denom = _get_text(anagrafica, 'Denominazione')
                    if denom:
                        data.cedente_denominazione = denom
                    else:
                        nome = _get_text(anagrafica, 'Nome')
                        cognome = _get_text(anagrafica, 'Cognome')
                        data.cedente_denominazione = f"{cognome} {nome}".strip()

            data.cedente_riferimento_amministrazione = _get_text(
                cedente, 'RiferimentoAmministrazione'
            )

        # === BODY: uno o più FatturaElettronicaBody ===
        # Di solito c'è un solo Body per file; li gestiamo tutti accumulando dati
        bodies = _find_all(root, ['FatturaElettronicaBody'])
        if not bodies:
            data.parsing_errors.append("Nessun FatturaElettronicaBody trovato")
            return data

        # === DatiGeneraliDocumento ===
        for body in bodies:
            dati_gen_doc = _find_first(body, ['DatiGenerali', 'DatiGeneraliDocumento'])
            if dati_gen_doc is not None:
                if not data.tipo_documento:
                    data.tipo_documento = _get_text(dati_gen_doc, 'TipoDocumento')
                if not data.numero:
                    data.numero = _get_text(dati_gen_doc, 'Numero')
                if not data.data:
                    data.data = _get_text(dati_gen_doc, 'Data')
                if not data.divisa:
                    data.divisa = _get_text(dati_gen_doc, 'Divisa') or 'EUR'

                # Importo totale documento (opzionale nello standard)
                imp_tot = _get_text(dati_gen_doc, 'ImportoTotaleDocumento')
                if imp_tot:
                    data.importo_totale = _safe_float(imp_tot)

                # Causali (possono essere multiple)
                for child in dati_gen_doc:
                    if _strip_namespace(child.tag) == 'Causale' and child.text:
                        data.causali.append(child.text.strip())

            # === DatiOrdineAcquisto (possono essere multipli) ===
            for oda in _find_all(body, ['DatiGenerali', 'DatiOrdineAcquisto']):
                id_doc = _get_text(oda, 'IdDocumento')
                if id_doc:
                    data.oda_valori_grezzi.append(id_doc)
                    normalized = _normalize_oda(id_doc)
                    for n in normalized:
                        if n not in data.oda_riferimenti:
                            data.oda_riferimenti.append(n)
                    # Cerco anche commesse
                    for c in _extract_commessa_refs(id_doc):
                        if c not in data.commessa_riferimenti:
                            data.commessa_riferimenti.append(c)

            # === DatiContratto (fallback OdA) ===
            for contr in _find_all(body, ['DatiGenerali', 'DatiContratto']):
                id_doc = _get_text(contr, 'IdDocumento')
                if id_doc:
                    data.contratto_riferimenti.append(id_doc)
                    normalized = _normalize_oda(id_doc)
                    for n in normalized:
                        if n not in data.oda_riferimenti:
                            data.oda_riferimenti.append(n)

            # === DatiRicezione (fallback) ===
            for ric in _find_all(body, ['DatiGenerali', 'DatiRicezione']):
                id_doc = _get_text(ric, 'IdDocumento')
                if id_doc:
                    data.ricezione_riferimenti.append(id_doc)

            # === DatiBeniServizi/DatiRiepilogo (totali) ===
            for riep in _find_all(body, ['DatiBeniServizi', 'DatiRiepilogo']):
                imp_base = _safe_float(_get_text(riep, 'ImponibileImporto'))
                imposta = _safe_float(_get_text(riep, 'Imposta'))
                data.imponibile_totale += imp_base
                data.imposta_totale += imposta

            # Se non avevamo il totale documento, lo ricavo
            if not data.importo_totale:
                data.importo_totale = data.imponibile_totale + data.imposta_totale

            # === DettaglioLinee (righe) ===
            for linea in _find_all(body, ['DatiBeniServizi', 'DettaglioLinee']):
                line = FatturaPALine()
                line.numero_linea = int(_get_text(linea, 'NumeroLinea') or 0)
                line.descrizione = _get_text(linea, 'Descrizione')
                line.quantita = _safe_float(_get_text(linea, 'Quantita'))
                line.unita_misura = _get_text(linea, 'UnitaMisura')
                line.prezzo_unitario = _safe_float(_get_text(linea, 'PrezzoUnitario'))
                line.prezzo_totale = _safe_float(_get_text(linea, 'PrezzoTotale'))
                line.aliquota_iva = _safe_float(_get_text(linea, 'AliquotaIVA'))

                cod_art = _find_first(linea, ['CodiceArticolo'])
                if cod_art is not None:
                    line.codice_articolo_tipo = _get_text(cod_art, 'CodiceTipo')
                    line.codice_articolo_valore = _get_text(cod_art, 'CodiceValore')

                # OdA a livello di riga
                for oda_line in _find_all(linea, ['AltriDatiGestionali']):
                    tipo = _get_text(oda_line, 'TipoDato')
                    if tipo and 'ORD' in tipo.upper():
                        val = _get_text(oda_line, 'RiferimentoTesto')
                        if val:
                            normalized = _normalize_oda(val)
                            line.riferimenti_oda.extend(normalized)

                # Cerca OdA anche nel testo della descrizione riga
                # (per fornitori che scrivono "Ordine di acquisto P01234" liberamente)
                if line.descrizione:
                    textual_odas = _extract_oda_from_text(line.descrizione)
                    for oda in textual_odas:
                        if oda not in data.oda_riferimenti_testuali:
                            data.oda_riferimenti_testuali.append(oda)

                    # Estrai POD/PDR (utility energia/gas) — usati come chiave
                    # multi-contratto per fornitori tipo Sorgenia/A2A/Enel.
                    for pod in _extract_pods(line.descrizione):
                        if pod not in data.pod_riferimenti:
                            data.pod_riferimenti.append(pod)

                data.righe.append(line)

        # Cerco commesse anche nelle causali
        for causale in data.causali:
            for c in _extract_commessa_refs(causale):
                if c not in data.commessa_riferimenti:
                    data.commessa_riferimenti.append(c)
            # Cerco anche OdA nelle causali (testo libero)
            textual_odas = _extract_oda_from_text(causale)
            for oda in textual_odas:
                if oda not in data.oda_riferimenti_testuali:
                    data.oda_riferimenti_testuali.append(oda)

    except ET.ParseError as e:
        data.parsing_errors.append(f"Errore parsing XML: {e}")
    except Exception as e:
        data.parsing_errors.append(f"Errore imprevisto: {e}")

    return data


def parse_from_base64(b64_content: str) -> FatturaPAData:
    """Parsa un XML ricevuto in base64 (come dal campo 'datas' di Odoo)."""
    try:
        xml_bytes = base64.b64decode(b64_content)
        xml_text = xml_bytes.decode('utf-8', errors='replace')
        return parse_fatturapa_xml(xml_text)
    except Exception as e:
        data = FatturaPAData()
        data.parsing_errors.append(f"Errore decodifica base64: {e}")
        return data
