"""
Parser PDF allegato fattura Enilive S.p.A. (IT11403240960).

Differenza architetturale rispetto a Edenred UTA: l'XML FatturaPA di Enilive
contiene solo righe aggregate per prodotto (BENZSP/DIESEL/ADBLUE/FEE), NON
ha <RiferimentoAmministrazione> per riga. Il dettaglio carta-per-carta è
disponibile SOLO nel PDF allegato (<Allegati><Attachment> base64).

Questo modulo:
  1) Estrae il PDF embedded dal raw_xml FatturaPA
  2) Parsa col regex i blocchi "Totale carta: <17 cifre> ..." per ogni carta
  3) Estrae anche la riga "FEE SICUREZZA E GEST" (voce di servizio)
  4) Aggrega per classificazione fiscale via CARTE_ENILIVE_BY_NUMERO

Output: EniliveBreakdown utilizzabile dal writer `create_bozza_enilive`.
"""
import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


# ============================================================
# DATACLASS RISULTATO
# ============================================================

@dataclass
class CartaInfo:
    """Subtotale per singola carta estratto dal PDF."""
    numero_carta: str
    totale_lordo: float
    totale_netto: float
    imponibile: float
    iva: float
    classificazione: Optional[str] = None   # POOL/uso_promiscuo/super_lusso o None


@dataclass
class FeeServizio:
    """Riga FEE SICUREZZA E GEST (servizio non legato a carte)."""
    totale: float
    imponibile: float
    iva: float


@dataclass
class EniliveBreakdown:
    """Aggregato completo derivato dal PDF Enilive."""
    carte: List[CartaInfo] = field(default_factory=list)
    fee_sicurezza: Optional[FeeServizio] = None
    carte_non_in_mappa: List[str] = field(default_factory=list)

    @property
    def imponibile_carte(self) -> float:
        return round(sum(c.imponibile for c in self.carte), 2)

    @property
    def imponibile_totale_calcolato(self) -> float:
        tot = self.imponibile_carte
        if self.fee_sicurezza:
            tot += self.fee_sicurezza.imponibile
        return round(tot, 2)

    def aggregate_by_classe(self) -> Dict[str, Dict]:
        """Raggruppa carte per classificazione.

        Ritorna dict classe -> {'imponibile': float, 'iva': float, 'totale': float,
                                  'carte': List[str]}.
        Le carte senza classificazione (non in mappa) finiscono in '_NON_IN_MAPPA'.
        """
        agg: Dict[str, Dict] = {}
        for c in self.carte:
            key = c.classificazione or '_NON_IN_MAPPA'
            bucket = agg.setdefault(key, {
                'imponibile': 0.0, 'iva': 0.0, 'totale': 0.0, 'carte': [],
            })
            bucket['imponibile'] += c.imponibile
            bucket['iva']        += c.iva
            bucket['totale']     += c.totale_lordo
            bucket['carte'].append(c.numero_carta)
        # Arrotondamenti
        for v in agg.values():
            v['imponibile'] = round(v['imponibile'], 2)
            v['iva']        = round(v['iva'], 2)
            v['totale']     = round(v['totale'], 2)
        return agg


# ============================================================
# REGEX DI ESTRAZIONE
# ============================================================

# Esempio:
# "Totale carta: 710200185924000012 170,95 170,95 140,13 30,82"
# colonne:        <numero>          <lordo> <netto> <imponib.> <iva>
RE_TOTALE_CARTA = re.compile(
    r'Totale carta:\s*(\d{17,18})\s+'
    r'([\d.,]+)\s+'   # totale lordo
    r'([\d.,]+)\s+'   # totale netto
    r'([\d.,]+)\s+'   # imponibile
    r'([\d.,]+)',     # IVA
    re.IGNORECASE,
)

# Esempio (riga isolata nel riepilogo prodotti):
# "FEE SICUREZZA 7,32 6,00 22,00 1,32"
# colonne:        <totale> <imponib.> <%IVA> <IVA>
RE_FEE = re.compile(
    r'^FEE SICUREZZA\s+([\d.,]+)\s+([\d.,]+)\s+22,00\s+([\d.,]+)\s*$',
    re.MULTILINE,
)


def _to_float_it(s: str) -> float:
    """Numero italiano '1.234,56' -> float 1234.56."""
    return float(s.replace('.', '').replace(',', '.'))


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[1] if '}' in tag else tag


# ============================================================
# ESTRAZIONE PDF DA XML
# ============================================================

def extract_attached_pdf_from_xml(raw_xml: str) -> Optional[bytes]:
    """Estrae il primo PDF allegato dentro <Allegati><Attachment> del FatturaPA.

    Ritorna i bytes decodificati o None se non c'è allegato PDF.
    """
    if not raw_xml:
        return None
    try:
        root = ET.fromstring(raw_xml.encode('utf-8') if isinstance(raw_xml, str)
                              else raw_xml)
    except ET.ParseError as e:
        logger.error(f"XML non parsabile: {e}")
        return None

    for elem in root.iter():
        if _strip_ns(elem.tag) != 'Allegati':
            continue
        nome = None
        b64 = None
        for child in elem:
            t = _strip_ns(child.tag)
            if t == 'NomeAttachment':
                nome = (child.text or '').strip()
            elif t == 'Attachment':
                b64 = (child.text or '').strip()
        if not b64:
            continue
        # Considero PDF se il nome finisce in .pdf, oppure il primo allegato
        # se Nome non specificato (Enilive usa sempre .pdf).
        if nome and not nome.lower().endswith('.pdf'):
            continue
        try:
            return base64.b64decode(b64)
        except Exception as e:
            logger.error(f"Allegato {nome!r} non decodificabile base64: {e}")
            continue
    return None


# ============================================================
# PARSER PDF
# ============================================================

def parse_enilive_pdf(pdf_bytes: bytes) -> EniliveBreakdown:
    """Parsa il PDF allegato Enilive e ritorna EniliveBreakdown.

    Usa pdfplumber per estrarre il testo, poi regex per i totali per carta
    e per la riga FEE SICUREZZA.

    Per la classificazione carta -> classe fiscale viene fatto lookup su
    CARTE_ENILIVE_BY_NUMERO (config/carte_enilive_mapping.py).
    """
    # Import lazy: pdfplumber è un'optional dep (già installata in prod ma
    # tienilo qui per evitare import cost se questo modulo non viene usato).
    import pdfplumber
    import io

    from config.carte_enilive_mapping import get_classificazione_carta_enilive

    if not pdf_bytes:
        raise ValueError("pdf_bytes vuoto")

    breakdown = EniliveBreakdown()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = '\n'.join((page.extract_text() or '') for page in pdf.pages)

    # 1) Subtotali per carta
    for m in RE_TOTALE_CARTA.finditer(full_text):
        numero = m.group(1)
        try:
            carta = CartaInfo(
                numero_carta=numero,
                totale_lordo=_to_float_it(m.group(2)),
                totale_netto=_to_float_it(m.group(3)),
                imponibile=_to_float_it(m.group(4)),
                iva=_to_float_it(m.group(5)),
            )
        except ValueError as e:
            logger.warning(f"Carta {numero}: impossibile parsare numeri ({e})")
            continue
        carta.classificazione = get_classificazione_carta_enilive(numero)
        if carta.classificazione is None:
            breakdown.carte_non_in_mappa.append(numero)
        breakdown.carte.append(carta)

    # 2) FEE SICUREZZA E GEST (può non esserci in tutte le fatture)
    fee_match = RE_FEE.search(full_text)
    if fee_match:
        try:
            breakdown.fee_sicurezza = FeeServizio(
                totale=_to_float_it(fee_match.group(1)),
                imponibile=_to_float_it(fee_match.group(2)),
                iva=_to_float_it(fee_match.group(3)),
            )
        except ValueError as e:
            logger.warning(f"FEE SICUREZZA: parsing fallito ({e})")

    return breakdown
