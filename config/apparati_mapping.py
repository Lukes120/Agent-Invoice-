"""
Mappa apparato -> classificazione deducibilità (Ecotel).

AUTO-GENERATO da scripts/generate_apparati_mapping.py
Sorgente: Apparati completo TOT.xlsx (foglio "Totale")
Generato: 2026-05-08 11:27:09
Apparati totali: 75
  - distribuzione per (stato, classificazione):
    MAPPATO / furgoni: 47
    MAPPATO / uso_promiscuo: 28
Skipped (stato non ammesso): 0
Duplicati intra-cc (tenuta l'ultima): 0
Apparati multi-cc (cross-contratto): 0

Stati: MAPPATO/CENSITO=attivi, DISMESSO/GUASTA=storici (mantenuti
per consentire la classificazione di fatture pregresse).

NON modificare a mano: rigenerare con
    python scripts/generate_apparati_mapping.py
"""
from typing import Optional, List

CLASSIFICAZIONE_USO_PROMISCUO = "uso_promiscuo"
CLASSIFICAZIONE_FURGONI = "furgoni"

STATI_ATTIVI = {"MAPPATO", "CENSITO"}
STATI_STORICI = {"DISMESSO", "GUASTA"}

# Mappa codice_apparato -> metadati
APPARATI_MAP = {
    "1007908294": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'VOLKSWAGEN T-Roc 1 T-ROC 2.0',
        "targa": "GY711VS",
        "classificazione": "uso_promiscuo",
    },
    "1007908344": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'FIAT DUCATO 3 EASY PRO 2.2',
        "targa": "GP641KX",
        "classificazione": "furgoni",
    },
    "1007932062": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'ALFA ROMEO STELVIO (PC) 2.2 TURBO DIESEL',
        "targa": "GD974AZ",
        "classificazione": "uso_promiscuo",
    },
    "1007932195": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008',
        "targa": "HC468CT",
        "classificazione": "uso_promiscuo",
    },
    "1007932633": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008 Hybrid',
        "targa": "HC444CS",
        "classificazione": "uso_promiscuo",
    },
    "1007932708": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'BOXER 2.2 BLUEHDI 140 S&S 333 L2H2',
        "targa": "GW162RR",
        "classificazione": "furgoni",
    },
    "1007932955": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'RENAULT VAN MASTER 22T',
        "targa": "GP454SF",
        "classificazione": "furgoni",
    },
    "1007932989": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'BMW X3 xDrive20d 48V',
        "targa": "GN384RB",
        "classificazione": "uso_promiscuo",
    },
    "1007933011": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'ALFA ROMEO TONALE 1.6 Diesel 130cv TCT6 Sprint',
        "targa": "GW429NN",
        "classificazione": "uso_promiscuo",
    },
    "1007933086": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'IVECO DAILY',
        "targa": "GJ531GR",
        "classificazione": "furgoni",
    },
    "1007933102": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008',
        "targa": "HC466CT",
        "classificazione": "uso_promiscuo",
    },
    "1007933417": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008 Hybrid',
        "targa": "HC439CS",
        "classificazione": "uso_promiscuo",
    },
    "1007937327": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'DUCATO 35 LH2 2.2 Mjt3 140CV AT9 E6D-fin - Diesel',
        "targa": "GT281SG",
        "classificazione": "furgoni",
    },
    "1007937350": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'PEUGEOT BOXER CARGO VAN',
        "targa": "GW197RR",
        "classificazione": "furgoni",
    },
    "1007953019": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008 Hybrid',
        "targa": "HC479CS",
        "classificazione": "uso_promiscuo",
    },
    "270557275": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'OPEL Frontera Hybrid HA318MN',
        "targa": "HA318MN",
        "classificazione": "uso_promiscuo",
    },
    "272080268": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'Pool',
        "targa": "GS675LK",
        "classificazione": "furgoni",
    },
    "285642252": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'FIAT SCUDO L2H1 1.5 BlueHdi 120cv MT6 LOUNGE GM211WK',
        "targa": "GM211WK",
        "classificazione": "furgoni",
    },
    "285870531": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Apcoa, Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'VOLKSWAGEN T-ROC GM066DD',
        "targa": "GM066DD",
        "classificazione": "uso_promiscuo",
    },
    "286352315": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'FIAT DUCATO GR764LY',
        "targa": "GR764LY",
        "classificazione": "furgoni",
    },
    "286611587": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'PEUGEOT 2008 GP617XJ',
        "targa": "GP617XJ",
        "classificazione": "uso_promiscuo",
    },
    "286616925": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "287783070": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "290878933": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'BMW X4 hybrid 20d GW950EK',
        "targa": "GW950EK",
        "classificazione": "uso_promiscuo",
    },
    "291857324": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'FIAT DUCATO GR137HC',
        "targa": "GR137HC",
        "classificazione": "furgoni",
    },
    "292714227": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'FIAT 500 X GN550NJ',
        "targa": "GN550NJ",
        "classificazione": "furgoni",
    },
    "292762283": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'FIAT DOBLO VAN CH1 1.5 BlueHdi 100cv MT6 GR402RX',
        "targa": "GR402RX",
        "classificazione": "furgoni",
    },
    "305658973": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'IVECO',
        "targa": "EW701LS",
        "classificazione": "furgoni",
    },
    "305734873": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'PEUGEOT BOXER 2.2 BLUEHDI 140 S&S 333 L2H2',
        "targa": "GW938RP",
        "classificazione": "furgoni",
    },
    "305828709": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'FIAT DUCATO 35Q LH2 140CV 2.2 Multijet 3 E6E',
        "targa": "HA035DD",
        "classificazione": "furgoni",
    },
    "305835175": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'JEEP Avenger Hybrid',
        "targa": "GZ985VY",
        "classificazione": "uso_promiscuo",
    },
    "305835266": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008 Hybrid',
        "targa": "GZ947ZN",
        "classificazione": "uso_promiscuo",
    },
    "305836348": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'MERCEDES GLC',
        "targa": "GX358YF",
        "classificazione": "uso_promiscuo",
    },
    "305873770": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": '',
        "targa": "",
        "classificazione": "furgoni",
    },
    "305876187": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Mercedes GLE coupè',
        "targa": "HD446BE",
        "classificazione": "uso_promiscuo",
    },
    "305876286": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'JEEP Avenger Hybrid',
        "targa": "GZ214VZ",
        "classificazione": "furgoni",
    },
    "305876377": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'PEUGEOT 5008',
        "targa": "GW788RR",
        "classificazione": "uso_promiscuo",
    },
    "305876419": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'FIAT DUCATO 35Q LH2 140CV 2.2 Multijet 3 E6E',
        "targa": "HA570JN",
        "classificazione": "furgoni",
    },
    "305876435": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'JEEP Avenger Hybrid',
        "targa": "GX657WG",
        "classificazione": "uso_promiscuo",
    },
    "305876450": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'FIAT FIORINO 1.3 Multijet 95 CV E6d-final SX',
        "targa": "GN985WN",
        "classificazione": "furgoni",
    },
    "305876468": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'ALFA ROMEO JUNIOR Ibrida 1.2',
        "targa": "GW656NM",
        "classificazione": "uso_promiscuo",
    },
    "305876476": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'LANCIA Y4 Hybrid',
        "targa": "HA220JR",
        "classificazione": "uso_promiscuo",
    },
    "305876526": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'CITROEN C5 AIRCROSS',
        "targa": "GK683BC",
        "classificazione": "uso_promiscuo",
    },
    "305876542": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'IVECO',
        "targa": "DM581KH",
        "classificazione": "furgoni",
    },
    "305876583": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008 Hybrid',
        "targa": "HA737WH",
        "classificazione": "uso_promiscuo",
    },
    "305876625": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'CITROEN Berlingo',
        "targa": "HA491JP",
        "classificazione": "furgoni",
    },
    "305876658": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'FIAT DUCATO 35 MH2 2.2 Mjt3 140CV E6.4 Easy P',
        "targa": "GN915GX",
        "classificazione": "furgoni",
    },
    "305904666": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'PEUGEOT 3008',
        "targa": "GR895KR",
        "classificazione": "furgoni",
    },
    "305904716": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'PEUGEOT BOXER 335 L3H2 2.0 BlueHDi 160cv Cab.App',
        "targa": "GV030VF",
        "classificazione": "furgoni",
    },
    "305908402": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'JEEP RENEGADE 1.6 Mjet',
        "targa": "GM637FW",
        "classificazione": "uso_promiscuo",
    },
    "305908410": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'Peugeot 3008 Hybrid',
        "targa": "GZ954ZN",
        "classificazione": "uso_promiscuo",
    },
    "305908428": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'TOYOTA Yaris - Cross',
        "targa": "GV342RW",
        "classificazione": "uso_promiscuo",
    },
    "305908493": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'FIORINO 1.3 Multijet 95 CV E6d-final SX',
        "targa": "GN267CW",
        "classificazione": "furgoni",
    },
    "307886804": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Apcoa, Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'PEUGEOT 208 Allure Pack BlueHDi 100 S/S GN780EW',
        "targa": "GN780EW",
        "classificazione": "uso_promiscuo",
    },
    "320645682": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "359782019": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "359782022": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "359782023": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "359782026": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "361155749": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
        "note": 'guasta',
    },
    "361155826": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "361155829": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "361155901": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "311531633",
        "veicolo_descrizione": 'NISSAN TOWNSTAR (FURGONE)',
        "targa": "GL999BT",
        "classificazione": "furgoni",
    },
    "366282461": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'Pool',
        "targa": "GP642XH",
        "classificazione": "furgoni",
    },
    "369426477": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "385592119": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'viacard targa GP642XH',
        "targa": "GP642XH",
        "classificazione": "furgoni",
    },
    "385592121": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "385592122": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "385592123": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
    },
    "385592125": {
        "stato": "MAPPATO",
        "tipo_apparato": "VIACARD",
        "fornitori": 'Autostrade',
        "cc_cliente": "261713569",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
        "note": 'guasta',
    },
    "950197772": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'FIAT PANDA  HYBRID 1.0 GL898ZH',
        "targa": "GL898ZH",
        "classificazione": "furgoni",
    },
    "972699706": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'DS 7 Blue HDI 130 Automatica Pallas 5D Sport GX338PH',
        "targa": "GX338PH",
        "classificazione": "uso_promiscuo",
    },
    "974984999": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Apcoa, Autostrade',
        "cc_cliente": "216875601",
        "veicolo_descrizione": 'Pool',
        "targa": "GM112NK",
        "classificazione": "furgoni",
    },
    "975920398": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'FIAT DUCATO 28 CH1 2.2 Mjt3 120CV E6D-fin GM112NK',
        "targa": "GM112NK",
        "classificazione": "furgoni",
    },
    "976768150": {
        "stato": "MAPPATO",
        "tipo_apparato": "TELEPASS",
        "fornitori": 'Autostrade',
        "cc_cliente": "217718183",
        "veicolo_descrizione": 'Pool',
        "targa": "",
        "classificazione": "furgoni",
        "note": 'guasta',
    },
}

# Alias: codici "corti" come appaiono nei PDF Autostrade
APPARATI_ALIAS = {
}

# Indice inverso targa -> list[apparato_id]
TARGA_INDEX = {}
for _aid, _e in APPARATI_MAP.items():
    _t = _e.get("targa")
    if _t:
        TARGA_INDEX.setdefault(_t, []).append(_aid)

# Indice inverso cc_cliente -> list[apparato_id]
CC_INDEX = {}
for _aid, _e in APPARATI_MAP.items():
    _cc = _e.get("cc_cliente")
    if _cc:
        CC_INDEX.setdefault(_cc, []).append(_aid)
    for _cc_x in _e.get("cc_clienti_aggiuntivi", []):
        CC_INDEX.setdefault(_cc_x, []).append(_aid)


def normalize_apparato_lookup(raw: str) -> str:
    """Normalizza un codice apparato per lookup (allineato al generator)."""
    import re
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "VIACARD" in raw.upper():
        m = re.search(r"\b(\d+\.\d{6}\.\d+)\b", raw)
        if m:
            return m.group(1)
    no_spaces = re.sub(r"\s+", "", raw)
    digit_seqs = re.findall(r"\d{6,}", no_spaces)
    if digit_seqs:
        return max(digit_seqs, key=len)
    return ""


def _resolve_key(key: str) -> Optional[str]:
    if key in APPARATI_MAP:
        return key
    main = APPARATI_ALIAS.get(key)
    return main if main in APPARATI_MAP else None


def get_classificazione(apparato_raw: str) -> Optional[str]:
    """Ritorna la classificazione ('uso_promiscuo'|'furgoni') o None."""
    key = normalize_apparato_lookup(apparato_raw)
    resolved = _resolve_key(key)
    return APPARATI_MAP[resolved]["classificazione"] if resolved else None


def get_apparato_info(apparato_raw: str):
    """Ritorna l'intera entry della mappa (dict) o None."""
    key = normalize_apparato_lookup(apparato_raw)
    resolved = _resolve_key(key)
    return APPARATI_MAP.get(resolved) if resolved else None


def get_apparati_by_targa(targa: str) -> List[str]:
    return list(TARGA_INDEX.get((targa or "").upper().strip(), []))


def get_apparati_by_cc(cc: str) -> List[str]:
    return list(CC_INDEX.get((cc or "").strip(), []))
