"""Test del parser FatturaPA con XML sintetici."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.fatturapa_parser import parse_fatturapa_xml, _normalize_oda

# Test 1: XML standard con P04532
xml1 = """<?xml version="1.0" encoding="UTF-8"?>
<p:FatturaElettronica xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2" versione="FPR12">
<FatturaElettronicaHeader>
  <CedentePrestatore>
    <DatiAnagrafici>
      <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>12345678901</IdCodice></IdFiscaleIVA>
      <Anagrafica><Denominazione>ARROW ECS SRL</Denominazione></Anagrafica>
    </DatiAnagrafici>
  </CedentePrestatore>
</FatturaElettronicaHeader>
<FatturaElettronicaBody>
  <DatiGenerali>
    <DatiGeneraliDocumento>
      <TipoDocumento>TD01</TipoDocumento>
      <Divisa>EUR</Divisa>
      <Data>2026-04-15</Data>
      <Numero>FT-2026/001</Numero>
      <ImportoTotaleDocumento>1220.00</ImportoTotaleDocumento>
      <Causale>Fornitura materiali</Causale>
    </DatiGeneraliDocumento>
    <DatiOrdineAcquisto>
      <IdDocumento>P04532</IdDocumento>
    </DatiOrdineAcquisto>
  </DatiGenerali>
  <DatiBeniServizi>
    <DettaglioLinee>
      <NumeroLinea>1</NumeroLinea>
      <Descrizione>Bulloni M6</Descrizione>
      <Quantita>100.00</Quantita>
      <PrezzoUnitario>10.00</PrezzoUnitario>
      <PrezzoTotale>1000.00</PrezzoTotale>
      <AliquotaIVA>22.00</AliquotaIVA>
    </DettaglioLinee>
    <DatiRiepilogo>
      <AliquotaIVA>22.00</AliquotaIVA>
      <ImponibileImporto>1000.00</ImponibileImporto>
      <Imposta>220.00</Imposta>
    </DatiRiepilogo>
  </DatiBeniServizi>
</FatturaElettronicaBody>
</p:FatturaElettronica>"""

# Test 2: Formato sporco "P04368 - 26.03.2026"
xml2 = xml1.replace('<IdDocumento>P04532</IdDocumento>',
                    '<IdDocumento>P04368 - 26.03.2026</IdDocumento>')

# Test 3: Commessa S03146 senza OdA vero
xml3 = xml1.replace('<IdDocumento>P04532</IdDocumento>',
                    '<IdDocumento>comm: S03146 ATM SAN</IdDocumento>')

# Test 4: Senza DatiOrdineAcquisto
import re as _re
xml4 = _re.sub(
    r'<DatiOrdineAcquisto>.*?</DatiOrdineAcquisto>',
    '', xml1, flags=_re.DOTALL
)

# Test 5: Trasporto in una riga aggiuntiva
xml5 = xml1.replace(
    '</DatiBeniServizi>',
    """<DettaglioLinee>
      <NumeroLinea>2</NumeroLinea>
      <Descrizione>Spese di trasporto</Descrizione>
      <Quantita>1.00</Quantita>
      <PrezzoUnitario>50.00</PrezzoUnitario>
      <PrezzoTotale>50.00</PrezzoTotale>
      <AliquotaIVA>22.00</AliquotaIVA>
    </DettaglioLinee>
    </DatiBeniServizi>"""
)

tests = [
    ("1) P+5cifre pulito", xml1, ["P04532"], []),
    ("2) P+5cifre sporco con data", xml2, ["P04368"], []),
    ("3) Commessa S, nessun OdA", xml3, [], ["S03146"]),
    ("4) Senza OdA", xml4, [], []),
    ("5) Con riga trasporto", xml5, ["P04532"], []),
]

all_ok = True
for title, xml, expected_oda, expected_commesse in tests:
    print(f"\n{title}")
    d = parse_fatturapa_xml(xml)
    print(f"  Denominazione: {d.cedente_denominazione}")
    print(f"  Numero: {d.numero} | Data: {d.data}")
    print(f"  Totale: €{d.importo_totale} | Imponibile: €{d.imponibile_totale}")
    print(f"  OdA rif: {d.oda_riferimenti} (grezzi: {d.oda_valori_grezzi})")
    print(f"  Commesse: {d.commessa_riferimenti}")
    print(f"  Righe: {len(d.righe)}")
    for line in d.righe:
        print(f"    #{line.numero_linea} '{line.descrizione}' qty={line.quantita} €{line.prezzo_totale}")
    if d.parsing_errors:
        print(f"  ERRORI: {d.parsing_errors}")

    ok_oda = sorted(d.oda_riferimenti) == sorted(expected_oda)
    ok_comm = sorted(d.commessa_riferimenti) == sorted(expected_commesse)
    status = "OK" if (ok_oda and ok_comm) else "FAIL"
    print(f"  => {status}")
    if not (ok_oda and ok_comm):
        all_ok = False
        print(f"     atteso OdA={expected_oda}, commesse={expected_commesse}")

print("\n" + ("TUTTI I TEST PASSATI" if all_ok else "CI SONO FALLIMENTI"))

# Test normalizzazione
print("\n--- Test _normalize_oda ---")
samples = [
    "P04368 - 26.03.2026",
    "comm: S03146 ATM SAN",
    "Off.479/RM",
    "N.A.",
    "P04532",
    "P04368, P04369",
    "PO-12345",
    "ORD/1234",
]
for s in samples:
    print(f"  '{s}' -> {_normalize_oda(s)}")
