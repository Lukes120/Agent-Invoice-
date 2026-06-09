"""
Regole di matching e tolleranze.
Modificare i valori secondo le policy contabili aziendali.
"""

# ============================================================
# TOLLERANZE PER MATCH FATTURA <-> ORDINE DI ACQUISTO
# ============================================================

TOLLERANZA_PERCENTUALE = 5.0   # % di scostamento accettato sul totale riga
TOLLERANZA_ASSOLUTA = 25.00    # € di scostamento accettato sul totale riga
TOLLERANZA_TOTALE_FATTURA = 25.00  # € di scostamento accettato sul totale fattura

# ============================================================
# MATCH IMPLICITO (OdA dedotto da fornitore + importo)
# ============================================================
# Usato quando la fattura non cita l'OdA nell'XML ma esiste un OdA
# non fatturato dello stesso fornitore con importo corrispondente.

# Tolleranza ASSOLUTA (€) per considerare il match implicito affidabile.
# Tenerla MOLTO stretta: match dedotti da coincidenze numeriche richiedono
# match al centesimo per evitare falsi positivi.
# Default 0.01 = match esatto al centesimo.
TOLLERANZA_MATCH_IMPLICITO_ASSOLUTA = 0.01

# Tolleranza percentuale (backup, per importi grandi).
# Un delta di 0.01 * importo * percentuale diventa rilevante solo sopra soglia.
TOLLERANZA_MATCH_IMPLICITO_PERCENT = 0.0

# Tolleranza LARGA per il match implicito adattivo.
# Se la ricerca stretta (al centesimo) NON trova candidati, si riprova
# con questa tolleranza più ampia. Il match viene accettato SOLO se
# trova esattamente 1 candidato univoco, altrimenti skip per evitare
# falsi positivi da coincidenze numeriche.
#
# VALORI CALIBRATI SU DATI REALI (post-incidente HILTI):
# - Assoluta €0.01 (praticamente al centesimo)
# - Percentuale 0.05% (cioè 5 centesimi per migliaio)
# Esempi pratici di cosa tollera:
#   OdA €100   -> ±€0.05
#   OdA €800   -> ±€0.40  (recupera AGN €0,10)
#   OdA €10000 -> ±€5.00
# Esempi di cosa NON tollera:
#   OdA €310 vs fattura €310,58 (diff €0,58 = 0,19% > 0,05%) - HILTI caso
TOLLERANZA_MATCH_IMPLICITO_LARGA_ASSOLUTA = 0.01
TOLLERANZA_MATCH_IMPLICITO_LARGA_PERCENT = 0.05

# Se True, quando 2+ fatture della stessa esecuzione puntano allo stesso
# OdA implicito, entrambe vengono declassate a MATCH_IMPLICITO_AMBIGUO
# per sicurezza. L'utente deve scegliere quale delle due.
MATCH_IMPLICITO_GUARDIA_DUPLICATI = True

# Abilita/disabilita globalmente il match implicito
MATCH_IMPLICITO_ATTIVO = True

# ============================================================
# MATCH PARZIALE (OdA dedotto da sottoinsieme di righe fattura)
# ============================================================
# Usato come ultima risorsa: fattura senza OdA, totale non matcha,
# ma un sottoinsieme delle righe corrisponde esattamente a un OdA
# dello stesso fornitore. Le righe escluse sono "extra".

# Abilita/disabilita il match parziale
MATCH_PARZIALE_ATTIVO = True

# Numero massimo di righe fattura per applicare il match parziale.
# Oltre, i sottoinsiemi esplodono (2^N) e la performance ne risente.
# 2^20 ≈ 1M sottoinsiemi → ~0.5s per fattura, ancora gestibile.
# Alzato da 12 a 20 il 2026-05-03 dopo audit: sblocca ~7 fatture/run
# senza rischiare timeout (es. Edilnovelli 17 righe, Wuerth 23 → 23 fuori).
MATCH_PARZIALE_MAX_RIGHE = 20

# Soglia massima: una singola riga "extra" non può essere > X% dell'imponibile.
# Sopra questa soglia, match scartato per prudenza (una riga grossa tolta
# dal match è sospetta, probabilmente non è "extra").
MATCH_PARZIALE_MAX_EXTRA_PERCENT = 50.0

# Tolleranza sul confronto sottoinsieme vs OdA (al centesimo).
MATCH_PARZIALE_TOLLERANZA_ASSOLUTA = 0.01

# ============================================================
# SUGGERIMENTI OdA (per NO_ODA)
# ============================================================
# Quando una fattura cade in NO_ODA (nessun riferimento, nessun match),
# l'agent cerca OdA aperti del fornitore dove un sottoinsieme di righe OdA
# somma l'imponibile fattura. Se trova, classifica come NO_ODA_CON_SUGGERIMENTI
# e propone i candidati per la registrazione manuale guidata.

# Abilita/disabilita suggerimenti
SUGGERIMENTI_ATTIVI = True

# Numero massimo di righe OdA per applicare la ricerca sottoinsieme.
# Oltre, skip (evita esplosione combinatoria).
SUGGERIMENTI_MAX_RIGHE = 40

# Tolleranza assoluta per considerare un sottoinsieme "matchante"
SUGGERIMENTI_TOLLERANZA_ASSOLUTA = 0.01

# Età massima in mesi degli OdA considerati per i suggerimenti.
# OdA più vecchi di N mesi vengono ignorati (solitamente sono chiusi
# o obsoleti). Evita che un REXEL abbia 12 candidati tra cui OdA
# di 2 anni fa.
SUGGERIMENTI_MAX_AGE_MONTHS = 12

# Se ENTRAMBE le tolleranze sono superate -> DA_VERIFICARE
# Se almeno una è rispettata -> AUTO_VALIDABILE


# ============================================================
# RICONOSCIMENTO RIGHE EXTRA (TRASPORTO, SPESE)
# ============================================================
# Ogni regola è una tupla (radice_parola_chiave, conto_contabile, categoria)
# Il matching è case-insensitive e "contains" sulla descrizione riga fattura.

KEYWORD_RULES = [
    # Spese di trasporto - mapping sul conto dedicato
    ("trasport",   "CONTO_TRASPORTI", "TRASPORTO"),
    ("spedizion",  "CONTO_TRASPORTI", "TRASPORTO"),
    ("porto",      "CONTO_TRASPORTI", "TRASPORTO"),
    ("corrier",    "CONTO_TRASPORTI", "TRASPORTO"),
    ("consegn",    "CONTO_TRASPORTI", "TRASPORTO"),

    # Altre categorie note - estendere secondo esigenza
    ("bollo",      "CONTO_BOLLI",     "BOLLO"),
    ("imball",     "CONTO_IMBALLAGGI","IMBALLAGGIO"),
    ("conai",      "CONTO_CONAI",     "CONTRIBUTO"),
    ("incasso",    "CONTO_ONERI_BANCARI", "ONERI_BANCARI"),
    ("bonifico",   "CONTO_ONERI_BANCARI", "ONERI_BANCARI"),
]

# ============================================================
# MAPPING CONTI CONTABILI
# ============================================================
# Inserire i codici conto reali della tua Odoo (es. "6015000" o simili)

CONTI_CONTABILI = {
    "CONTO_TRASPORTI":    "DA_COMPILARE",  # es. "6015001" spese di trasporto
    "CONTO_BOLLI":        "DA_COMPILARE",
    "CONTO_IMBALLAGGI":   "DA_COMPILARE",
    "CONTO_CONAI":        "DA_COMPILARE",
    "CONTO_DIFFERENZE":   "DA_COMPILARE",  # differenze di acquisto entro tolleranza
}


# ============================================================
# MAPPING ID ODOO PER RIGHE ACCESSORIE (POL extra su OdA)
# ============================================================
# Quando il classifier rileva MATCH_PARZIALE_OK con righe extra (spese
# trasporto, bolli, oneri bancari) il writer aggiunge una purchase.order.line
# dedicata sull'OdA per ogni riga extra (pattern operatore: 100% delle
# fatture posted ha le accessorie collegate a una POL, mai righe libere
# nel move). Per Ecotel (company_id=1).
#
# Categoria → dict con:
#   account_id: id account.account contabile (override su move_line)
#   product_id: id product.product (service generico)
#   description_prefix: prefisso opzionale per la descrizione POL
EXTRA_POL_MAPPING_ECOTEL = {
    "TRASPORTO": {
        "account_id": 125,    # 420110 costi di trasporto e spedizione
        "product_id": 12285,  # service "spese di trasporto"
    },
    "BOLLO": {
        "account_id": 160,    # 490100 valori bollati e imposte diverse
        "product_id": 12302,  # service "BOLLO"
    },
    "ONERI_BANCARI": {
        "account_id": 134,    # 420410 oneri bancari
        "product_id": 22859,  # service "Spese Incasso"
    },
    "IMBALLAGGIO": {
        "account_id": 125,    # fallback su trasporto/spedizione (no conto dedicato)
        "product_id": 12285,
    },
    "CONTRIBUTO": {
        "account_id": 125,    # fallback (no conto dedicato CONAI in Ecotel)
        "product_id": 12285,
    },
    # Default quando la keyword non è riconosciuta: trattata come trasporto
    # (categoria di gran lunga più frequente, vedi audit storico).
    "_DEFAULT": {
        "account_id": 125,
        "product_id": 12285,
    },
}

# Mapping IVA → tax_id per le righe extra (acquisti, Ecotel).
# Il classifier prende l'aliquota dalla riga XML <AliquotaIVA>.
EXTRA_POL_TAX_BY_IVA_ECOTEL = {
    22.0: 6,   # 22% G
    10.0: 7,   # 10% G
    4.0:  58,  # 4%
    0.0:  None,  # esente: gestire caso per caso (richiede tax esente specifica)
}

# Toggle globale per attivare/disattivare l'aggiunta automatica POL extra.
# Quando False, MATCH_PARZIALE_OK con extra resta bloccato (comportamento legacy).
ADD_EXTRA_POL_TO_ODA_ENABLED = True

# Soglia massima per importo singola riga extra (sicurezza).
# Sopra questo importo, il writer rifiuta di aggiungere la POL automaticamente
# (probabile errore di classificazione o falso positivo).
ADD_EXTRA_POL_MAX_AMOUNT = 200.0

# Soglia (EUR) sotto la quale il delta di MATCH_DA_SUGGERIMENTO_PIU_EXTRA è
# trattato come ARROTONDAMENTO: la riga viene agganciata all'OdA ereditando
# product/UoM/commessa/IVA dalla prima POL matchata e usa il conto merci
# c/acquisti (ROUNDING_ACCOUNT_ID_ECOTEL). Sopra la soglia il comportamento
# resta quello legacy (riga "Spese accessorie" su 420110, no aggancio OdA).
ROUNDING_THRESHOLD_AMOUNT = 1.0

# Conto contabile per arrotondamenti (Ecotel). 410100 = merci c/acquisti.
# Verificato 2026-05-04 via XML-RPC su prod.
ROUNDING_ACCOUNT_ID_ECOTEL = 115


# ============================================================
# PATTERN RIFERIMENTI ORDINE DI ACQUISTO
# ============================================================
# Regex per estrarre il numero OdA dai campi fattura XML

ODA_PATTERNS = [
    r"P\d{5}",          # formato P01234
    r"PO\d{4,6}",       # formato PO1234 o PO123456
    r"ORD[-/]?\d+",     # formato ORD-1234 o ORD/1234
]


# ============================================================
# PRIORITIZZAZIONE ECCEZIONI
# ============================================================
# Ordine di priorità per la lista "da verificare"

PRIORITY_WEIGHTS = {
    "importo":      0.4,   # più alto = più prioritario
    "anzianita":    0.3,   # fatture più vecchie = più prioritarie
    "fornitore":    0.2,   # fornitori critici = più prioritari
    "scostamento":  0.1,   # scostamento % maggiore = più prioritario
}

FORNITORI_CRITICI = [
    # Inserire VAT o nome dei fornitori con cui serve attenzione extra
    # es. "IT12345678901",
]


# ============================================================
# PARAMETRI ESECUZIONE
# ============================================================

# Giorni indietro da analizzare se non specificato range
DEFAULT_LOOKBACK_DAYS = 30

# Stati fattura da considerare (in Odoo 14: 'draft', 'posted', 'cancel')
# Noi vogliamo solo quelle NON ancora registrate
STATI_FATTURA_DA_ANALIZZARE = ['draft']


# ============================================================
# MAPPATURA FORNITORI FISSI (Fase 3)
# ============================================================
# Per alcuni fornitori ricorrenti ogni fattura va sempre sullo stesso OdA
# e sullo stesso conto contabile. Quando l'agent trova una fattura NO_ODA
# di uno di questi fornitori, la classifica come MAPPATURA_FORNITORE_FISSO
# e propone l'OdA + conto direttamente.
#
# VALIDAZIONE: la mappatura è stata derivata analizzando lo storico fatture
# registrate (account.move) nel periodo 15/01/2026 - 19/04/2026.
# Solo fornitori dove il 100% delle fatture ha stesso OdA + stesso conto.
#
# CHIAVE: P.IVA del fornitore (con prefisso IT)
# VALORI: dict con oda_fisso, conto_contabile, note

MAPPATURA_FORNITORI_FISSI = {
    # Trenitalia: 39 fatture storiche, 100% su P03524 + conto 420173
    # WRITE ENABLED: pattern validato, OdA attivo con righe libere
    'IT05403151003': {
        'nome': 'Trenitalia S.p.A.',
        'oda_fisso': 'P03524',
        'conto_contabile': '420173',
        'conto_descrizione': 'costi per spese di viaggio personale in trasferta',
        # Campi tecnici Odoo (per scrittura bozze)
        'partner_id': 1198,
        'conto_contabile_id': 442,       # conto 420173 per company Ecotel
        'taxes_id': [12],                # IVA 10% (tipica trasporti)
        'journal_id': 2,                 # Fatture fornitore ACQ
        'company_id': 1,                 # Ecotel Italia
        # Strategia ricerca righe libere:
        #   'standard_qty_inv_rec' = qty_invoiced=0 AND qty_received=0 AND product_qty>=1
        #   (ignora prezzo e descrizione; adatta per OdA-ledger ricorrenti)
        'libere_criterio': 'standard_qty_inv_rec',
        # Strategia costruzione descrizione riga OdA a partire dall'XML:
        #   'trenitalia_titoli' = TRATTA + codici biglietti da AltriDatiGestionali + data
        'description_strategy': 'trenitalia_titoli',
        'auto_write_enabled': True,      # l'agent può creare bozze
    },
    # Italo NTV: fatture su P04279 + conto 420173
    # WRITE ENABLED: P04279 attivo, righe libere predisposte (price=1, descr "test")
    'IT09247981005': {
        'nome': 'Italo - Nuovo Trasporto Viaggiatori S.p.A.',
        'oda_fisso': 'P04279',
        'conto_contabile': '420173',
        'conto_descrizione': 'costi per spese di viaggio personale in trasferta',
        'partner_id': 1650,
        'conto_contabile_id': 442,
        'taxes_id': [12],
        'journal_id': 2,
        'company_id': 1,
        'libere_criterio': 'standard_qty_inv_rec',
        # 'pass_through' = descrizione XML riga già contiene codice+tratta
        # (es. "ZH91WG - Roma Termini - Milano Centrale"), aggiungo solo la data
        'description_strategy': 'pass_through',
        'auto_write_enabled': True,
    },
    # Telecom Italia: multi-contratto, ogni contratto → OdA dedicato
    # Conto 420310 (costi telefonici 80%) per tutti tranne P04107 (escluso)
    'IT00488410010': {
        'nome': 'Telecom Italia S.p.A.',
        'multi_contratto': True,
        'partner_id': 501,
        'conto_contabile': '420310',
        'conto_descrizione': 'costi telefonici deducibili 80%',
        'conto_contabile_id': 373,
        'journal_id': 2,
        'company_id': 1,
        'description_strategy': 'keep_original',
        'libere_criterio': 'standard_qty_inv_rec',
        'auto_write_enabled': True,
        # Indennità ritardato pagamento: crea nuova riga OdA con tax diversa
        'indennita_config': {
            'taxes_id': [47],
            'product_id': 12202,
        },
        'contratti': {
            '029091512': {
                'oda_fisso': 'P04056',
                'taxes_id': [11],
            },
            '888010937352': {
                'oda_fisso': 'P04516',
                'taxes_id': [11],
                # Multi-riga: righe XML vengono raggruppate e assegnate
                # a PO line diverse in base alla descrizione.
                # 'match': keyword cercata nella descrizione della PO line libera
                # 'is_residual': True = raccoglie righe XML non matchate da altri gruppi
                'line_groups': [
                    {'match': 'Contributo', 'is_residual': True},
                    {'match': 'Assistenza'},
                    {'match': 'Noleggio'},
                ],
            },
            '0213663221': {
                'oda_fisso': 'P04517',
                'taxes_id': [11],
            },
            '0613335859': {
                'oda_fisso': 'P04521',
                'taxes_id': [11],
            },
            '0613906836': {
                'oda_fisso': 'P04522',
                'taxes_id': [11],
            },
            '0613585854': {
                'oda_fisso': 'P04524',
                'taxes_id': [11],
            },
            '0613029315': {
                'oda_fisso': 'P04525',
                'taxes_id': [11],
            },
            '03513538009': {
                'oda_fisso': 'P04544',
                'taxes_id': [11],
            },
            # Censiti 26/05/2026 (verifica su Odoo da fattura registrata + storico origin).
            # 065914207 = contratto "ombrello" sede Roma Via Fiume Bianco 56: Telecom ci emette
            # fatture eterogenee (linea fissa, canoni). La contabilità le registra su P04107
            # (OdA Licenze MS 2026, righe libere €2.288) con conto 420310 + IVA 22%. Verificato
            # su fattura registrata 8W00207208 → ACQ/2026/2111.
            '065914207': {
                'oda_fisso': 'P04107',
                'taxes_id': [11],
            },
            # 093413049178 = Licenze Microsoft CSP, sede Roma. Va su P04956 (secondo OdA Licenze
            # MS 2026, righe libere mensili €800), confermato da utente.
            '093413049178': {
                'oda_fisso': 'P04956',
                'taxes_id': [11],
            },
            # NB pending NON mappati (registrazione manuale per ora, decisione 26/05):
            # - 0761356089 / 0761356106 (Viterbo) e 888012709459 (Carsoli) → calderone P03750,
            #   che NON ha righe libere (la contabilità crea una POL per fattura) ed è OdA 2025
            #   pieno: serve pattern "crea POL" + sostituto 2026 da Acquisti.
            # - 093413005566 (GCP €663) → una-tantum, nessuno storico.
        },
    },
    # Wind Tre: multi-contratto. Il routing al contratto avviene tramite
    # <RiferimentoAmministrazione> del CedentePrestatore (XML), NON tramite
    # <DatiContratto> come Telecom.
    # Conto 420310 (costi telefonici 80%), IVA 22% [11], stesso di Telecom.
    # P04758 (SIM 2026, rif 622057593) intenzionalmente NON mappato:
    # comportamento da chiarire dopo aver analizzato la fattura €512,40
    # (7649172819) che esce dal pattern bimestrale.
    'IT13378520152': {
        'nome': 'Wind Tre S.p.A.',
        'multi_contratto': True,
        'partner_id': 1386,
        'conto_contabile': '420310',
        'conto_descrizione': 'costi telefonici deducibili 80%',
        'conto_contabile_id': 373,
        'journal_id': 2,
        'company_id': 1,
        'description_strategy': 'keep_original',
        'libere_criterio': 'standard_qty_inv_rec',
        'auto_write_enabled': True,
        'contratti': {
            # Noleggio Terminale Mobile + SUPER Unlimited (bimestrale)
            '621989977': {
                'oda_fisso': 'P04759',
                'taxes_id': [11],
            },
            # NET RIDE 2026 (banda) — multi-riga: ogni bimestre la fattura
            # consuma sia la riga "GI05 - Canone NET RIDE IPNET" sia la riga
            # "Canone Servizio NET RIDE".
            '624792597': {
                'oda_fisso': 'P04754',
                'taxes_id': [11],
                'line_groups': [
                    {'match': 'GI05', 'is_residual': True},
                    {'match': 'Canone Servizio'},
                ],
            },
            # Professional Full €17,99/mese (12 righe ledger annuali).
            # Le fatture mensili portano N righe XML da €17,99 (una per mese
            # fatturato): consumano N righe libere distinte con prezzo identico.
            'P1109552094': {
                'oda_fisso': 'P04545',
                'taxes_id': [11],
                'lines_one_to_one': True,
            },
        },
    },
    # NWG Energia: OdA-ledger ricorrente P01178 con righe libere predisposte
    # (price=1, descrizione 'Test'). Il fornitore NON scrive l'OdA in fattura
    # (no DatiOrdineAcquisto), quindi il match implicito/parziale fallisce.
    # 1 contratto unico (IdContratto = 80613092687551), 1 riga IVA 22% per
    # bolletta mensile. Niente bollo nelle bollette periodiche.
    'IT02294320979': {
        'nome': 'NWG Energia S.p.A.',
        'oda_fisso': 'P01178',
        'conto_contabile': '420130',
        'conto_descrizione': 'costi per utenze magazzini',
        'partner_id': 1033,
        'conto_contabile_id': 126,
        'taxes_id': [11],                # IVA 22%
        'journal_id': 2,
        'company_id': 1,
        'libere_criterio': 'standard_qty_inv_rec',
        # Strategia descrizione: ricostruisce il pattern manuale storico
        # 'Fornitura <IdContratto> da DD/MM/YYYY a DD/MM/YYYY' (mese fattura)
        'description_strategy': 'nwg_periodo',
        'auto_write_enabled': True,
    },
    # WE4SERVICES Società Consortile a r.l.: OdA-ledger annuale P03696 con 4
    # righe placeholder per ogni mese (2× "Oneri Factoring" tax [54] esente +
    # 2× "FEE Maturata" tax [11] 22%). Le fatture NON citano mai l'OdA in XML
    # e gli importi sono variabili.
    # Routing fatture XML -> POL libera:
    #   1. mese di competenza = mese italiano della data fattura (es. 2026-05-08 -> "Maggio")
    #   2. tipo riga = keyword "Oneri Factoring" o "FEE" sulla descrizione XML
    # Le 2 POL gemelle per mese vengono consumate in ordine (FIFO sull'id POL).
    # Se entrambe sono già usate e arriva una 3ª fattura dello stesso tipo,
    # il writer ritorna errore e la fattura resta DA_VERIFICARE.
    # Storico (13 fatture posted gen-apr 2026): conto 525040 (id=1095), journal 2.
    # Chiave = P.IVA cedente da XML (IdPaese+IdCodice CedentePrestatore /
    # DatiAnagrafici), allineata con res.partner.vat su Odoo (id=585).
    # Il CodiceFiscale del CedentePrestatore (IT01641790702) è invece distinto.
    'IT14861711001': {
        'nome': 'WE4SERVICES Società Consortile a r.l.',
        'oda_fisso': 'P03696',
        'conto_contabile': '525040',
        'conto_descrizione': 'Commissioni WE4SERVICE',
        'partner_id': 585,
        'conto_contabile_id': 1095,
        'taxes_id': [11],                # default (FEE); il writer usa tax POL libera
        'journal_id': 2,
        'company_id': 1,
        'libere_criterio': 'standard_qty_inv_rec',
        # Strategia descrizione: mantiene il nome originale della POL libera
        # (es. "Oneri Factoring // Addebito Oneri Factoring Maggio") e appende
        # il numero fattura XML come riferimento ("... (rif.ft 70/01)").
        'description_strategy': 'keep_original_with_ref',
        'auto_write_enabled': True,
        # Nuova chiave: routing 2D (tipo + mese da data fattura). Il writer
        # multilinea cercherà 1 POL libera per ogni gruppo applicabile, con
        # AND fra keyword tipo e mese italiano della data fattura.
        'line_groups_by_month': [
            {'match': 'Oneri Factoring'},
            {'match': 'FEE'},
        ],
    },
    # SOCRATE SRLS: consulenze commerciali, 6 fatture posted negli ultimi 6 mesi
    # tutte da €18.300 esatti su P03366 (€128.100 ledger annuale, 9 righe da
    # €15.000 imponibile + IVA, una per mese). Storico conto: 420540 (4/6).
    # Pattern fisso mensile, nessuna variazione.
    'IT15317491007': {
        'nome': 'SOCRATE SRLS',
        'oda_fisso': 'P03366',
        'conto_contabile': '420540',
        'conto_descrizione': 'consulenze commerciali e provvigioni',
        'partner_id': 70560,
        'conto_contabile_id': 380,
        'taxes_id': [11],                # IVA 22% S
        'journal_id': 2,
        'company_id': 1,
        'libere_criterio': 'standard_qty_inv_rec',
        'description_strategy': 'pass_through',
        'auto_write_enabled': True,
    },
    # Sorgenia: multi-contratto (1 OdA-ledger per ogni POD/sede). Il routing
    # al contratto avviene tramite POD estratto dalle Descrizioni delle linee XML
    # (formato IT###L######## — nessun campo strutturato lo riporta).
    # Pattern: ogni fattura mensile aggrega più righe XML che vengono
    # contabilizzate su 1 sola riga libera del mese (ramo aggregato di
    # create_bozza_multilinea, no line_groups, no lines_one_to_one).
    'IT12874490159': {
        'nome': 'Sorgenia S.p.A.',
        'multi_contratto': True,
        'partner_id': 70672,
        'conto_contabile': '420130',
        'conto_descrizione': 'costi per utenze magazzini',
        'conto_contabile_id': 126,
        'journal_id': 2,
        'company_id': 1,
        'description_strategy': 'keep_original',
        'libere_criterio': 'standard_qty_inv_rec',
        'auto_write_enabled': True,
        'contratti': {
            # POD Treviolo
            'IT001E68725584': {
                'oda_fisso': 'P03434',
                'taxes_id': [11],
            },
            # POD Oricola
            'IT001E11450478': {
                'oda_fisso': 'P03407',
                'taxes_id': [11],
            },
        },
    },
    # AUTOSTRADE PER L'ITALIA S.P.A. — multi-contratto routing per codice cliente
    # (estratto dalla <Causale> XML "Codice cliente: NNN").
    # Pattern decifrato in plans/crispy-napping-tower.md sez. 3.2 (analisi v3).
    # Per i 4 cc main Ecotel: split A/B Furgoni (420160 100%) / Uso Promiscuo
    # (420840 70%) — il writer dedicato create_bozza_autostrade gestisce la
    # logica R4 (split automatico via PDF + APPARATI_MAP) o fallback R1
    # (2 righe vuote a importo 0, contabile inserisce manualmente).
    # I cc ex-Utterson e residuo sono stati chiusi (07/05/2026) — eventuali
    # fatture residue cadono in DA_VERIFICARE per gestione manuale.
    # TELEPASS S.P.A. — canoni Telepass mensili (Telepass-network)
    # Multi-contratto routing per codice cliente XML (causale "Codice cliente: NNN").
    # 4 cc Ecotel main → P03722. POL libere generiche "TEST €1 tx=11"
    # pre-create da Acquisti, riscritte completamente dall'agent al consumo.
    # Pattern decifrato 08/05/2026 da plans/hashed-marinating-kettle.md.
    # Voci attese sulle fatture XML: Canone Telepass / Canone T Business FAS S /
    # Parcheggi reselling / Quota associativa / Bollo. Routing conto contabile
    # via helper _classify_voce_telepass() nel writer.
    'IT09771701001': {
        'nome': 'Telepass S.p.A.',
        'multi_contratto': True,
        'partner_id': 1240,
        'journal_id': 2,
        'company_id': 1,
        'taxes_id': [11],                # 22% S default sui canoni
        'auto_write_enabled': False,     # NON attiva: validazione prima
        'description_strategy': 'telepass_canoni',
        # Conti contabili per voce (id Odoo Ecotel verificati prod)
        'conto_canone_id':     1124,     # 420840 pedaggi 70%
        'conto_parcheggio_id':  368,     # 420160 pedaggi 100%
        'conto_bollo_id':       160,     # 490100 valori bollati
        'contratti': {
            '311531633': {'oda_fisso': 'P03722', 'cc_type': 'telepass_main'},
            '216875601': {'oda_fisso': 'P03722', 'cc_type': 'telepass_main'},
            '261713569': {'oda_fisso': 'P03722', 'cc_type': 'telepass_main'},
            '217718183': {'oda_fisso': 'P03722', 'cc_type': 'telepass_main'},
        },
    },
    'IT07516911000': {
        'nome': 'Autostrade per l\'Italia S.p.A.',
        'multi_contratto': True,
        'partner_id': None,  # da censire al primo uso (ID Odoo verificato runtime)
        'journal_id': 2,
        'company_id': 1,
        # Conti split (id verificati Ecotel su prod, vedi piano v3 sez. 1.4)
        'conto_furgoni_id': 368,         # 420160 pedaggi 100%
        'conto_promiscuo_id': 1124,      # 420840 pedaggi 70%
        # Default per cc main: split A/B
        'taxes_id': [11],                # 22% S
        'auto_write_enabled': True,      # ATTIVA dal 07/05/2026 (consume-POL)
        'description_strategy': 'autostrade_main',
        'contratti': {
            # 4 cc Ecotel main → P03718 (vivo, 35 righe libere al 02/05/2026)
            '261713569': {
                'oda_fisso': 'P03718',
                'cc_type': 'ecotel_main',  # split A/B
            },
            '217718183': {
                'oda_fisso': 'P03718',
                'cc_type': 'ecotel_main',
            },
            '216875601': {
                'oda_fisso': 'P03718',
                'cc_type': 'ecotel_main',
            },
            '311531633': {
                'oda_fisso': 'P03718',
                'cc_type': 'ecotel_main',
            },
        },
    },
    # EDENRED UTA MOBILITY S.R.L. — carte carburante (~€200k/anno, 2 ft/mese)
    # XML: ~114 righe di rifornimenti singoli con <RiferimentoAmministrazione>
    # = numero carta UTA. Ogni riga -> 1 carta -> 1 veicolo -> classificazione
    # fiscale (POOL/uso_promiscuo/super_lusso). Aggregato in 3-4 voci semantiche
    # nel writer (clone pattern Athlon/Leasys).
    # Mappa carte in config/carte_carburante_mapping.py (rigenerata da
    # scripts/generate_carte_carburante_mapping.py a partire da
    # input/carte_uta.xlsx).
    # POL pre-pianificate da Acquisti su P03735 con name semantico
    # ("Costo carburante ft.n. XXX\n{classe}") -> keep_pol_name=True.
    'IT01696270212': {
        'nome': 'Edenred UTA Mobility S.r.l.',
        'partner_id': 1835,
        'oda_fisso': 'P03735',
        'multi_contratto': False,
        'description_strategy': 'edenred_uta',
        'keep_pol_name': True,
        'auto_write_enabled': False,   # cautela iniziale, da accendere dopo 1-2 fatture OK
        'journal_id': 2,
        'company_id': 1,
    },
    # ENILIVE S.P.A. — carte carburante (~€200k/anno, 1 ft/mese ca.)
    # XML: 5 righe AGGREGATE per prodotto (BENZSP/DIESEL/ADBLUE/FEE), NO
    # <RiferimentoAmministrazione>. Il dettaglio carta-per-carta è SOLO nel
    # PDF allegato. Lo parsa core/enilive_pdf_parser.py, classifica via
    # config/carte_enilive_mapping.py (input/carte_enilive.xlsx, auto-refresh
    # su mtime), e il writer create_bozza_enilive aggrega in max 3 voci
    # (POOL/uso_promiscuo/super_lusso) + 1 SERVIZIO (POL creata ad-hoc per la
    # FEE SICUREZZA E GEST, product_id 12202 "Fornitura di Servizi").
    # POL pre-pianificate quindicinali da Acquisti su P03731 con keyword
    # "AUTOMEZZI"/"AUTOVETTURE" e range "dal X al Y" -> match per periodo.
    'IT11403240960': {
        'nome': 'Enilive S.p.A.',
        'partner_id': 1908,
        'oda_fisso': 'P03731',
        'multi_contratto': False,
        'description_strategy': 'enilive_carte',
        'keep_pol_name': False,         # il writer riscrive il name della POL
        'auto_write_enabled': False,    # cautela iniziale, test reale prima
        'journal_id': 2,
        'company_id': 1,
    },
    # SARDA FACTORING S.P.A. — factoring (commissioni/interessi/spese).
    # Censito 26/05/2026 da analisi Odoo. Caratteristiche peculiari:
    #  - Nessun OdA citato in XML, nessuna riga libera su P03522: la contabilità
    #    CREA una nuova POL per ogni voce (pattern "crea POL", come Enilive).
    #  - Tutte le righe IVA 0%: esenti N4 (tax 54) tranne il bollo "RECUPERO
    #    IMPOSTA DI BOLLO" che è N1 art.15 (tax 47).
    #  - Conto unico 525020 (id 226), prodotto 12301, analytic 4177 (S03811).
    # Il writer dedicato create_bozza_factoring raggruppa le righe XML per IVA:
    # max 1 POL esente (somma) + 1 POL bollo (somma), descrizione = concatenazione
    # ' - ' delle descrizioni XML del gruppo. Solo TD01 (NC inesistenti → manuali).
    # Routing per P.IVA (flag 'factoring': True), endpoint /draft_factoring.
    'IT01681580922': {
        'nome': 'SARDA FACTORING S.P.A.',
        'partner_id': 51407,
        'oda_fisso': 'P03522',
        'multi_contratto': False,
        'factoring': True,                 # flag dispatch writer factoring
        'conto_contabile_id': 226,         # 525020 Commissioni SARDA FACTORING
        'product_id': 12301,               # COMMISSIONI FACTORING E INTERESSI
        'analytic_account_id': 4177,       # S03811 - Ecotel
        'taxes_esente': [54],              # N4 esenti (default righe)
        'taxes_bollo': [47],               # N1 escluse ex art.15 (righe "BOLLO")
        'auto_write_enabled': False,       # cautela iniziale, test reale prima
        'journal_id': 2,
        'company_id': 1,
    },
    # BLUE SGR S.P.A. — locazioni uffici Roma (2 immobili: IT0002, IT0011).
    # OdA-ledger annuale P03543 (S03811), 4 POL libere/mese: coppia Canone +
    # Acconto per ciascuna delle 2 proprietà. Routing: line_groups_by_month
    # cerca [Canone|Acconto] + mese italiano; le coppie sono equivalenti
    # (no disambiguation per proprietà). 2 fatture/mese (una per immobile),
    # importi variabili — il writer sovrascrive price_unit al momento della bozza.
    # Conti: Canone → 430110 (id 393), Acconto spese comuni → 430130 (id 395).
    # Fuori scope: AL-119 (imposta registro IVA 0%) e AL-120 (adeguamento ISTAT)
    # → registrazione manuale.
    'IT10219881009': {
        'nome': 'Blue Sgr Spa Fondo Alba',
        'partner_id': 1445,
        'oda_fisso': 'P03543',
        'conto_contabile_id': 393,         # 430110 locazione uffici (fallback)
        'taxes_id': [11],                  # IVA 22%
        'journal_id': 2,
        'company_id': 1,
        'libere_criterio': 'standard_qty_inv_rec',
        'line_groups_by_month': [
            {'match': 'Canone',  'account_id': 393},  # 430110 locazione uffici
            {'match': 'Acconto', 'account_id': 395},  # 430130 oneri accessori
        ],
        'description_strategy': 'keep_original',
        'auto_write_enabled': True,
    },
    # SANTINA SPA — parcheggi "Terminal Park" (Roma). OdA-ledger ricorrente
    # P04893 (commessa S03811) con righe placeholder price=1, identico schema
    # NWG Energia: il fornitore NON cita l'OdA in XML, importi variabili
    # (€10-24/fattura), 1 riga "Terminal Park ECOTEL ITALIA SRL." per fattura.
    # Storico: 7 fatture gia' registrate su P04893 da apr/2026, conto 420840
    # (pedaggi/parcheggi deducibili 70%, id 1124), IVA 22%. Cadevano in
    # NO_ODA_DA_CLASSIFICARE perche' non mappato. description_strategy
    # pass_through: usa la desc XML + data (distingue le righe nel ledger).
    # NB capienza: 6 righe libere su P04893 -> chiedere refill ad Acquisti
    # quando si esauriscono (~6 fatture).
    'IT00883111007': {
        'nome': 'SANTINA SPA',
        'oda_fisso': 'P04893',
        'conto_contabile': '420840',
        'conto_descrizione': 'pedaggi/parcheggi deducibili al 70%',
        'partner_id': 76253,
        'conto_contabile_id': 1124,
        'taxes_id': [11],                  # IVA 22%
        'journal_id': 2,
        'company_id': 1,
        'libere_criterio': 'standard_qty_inv_rec',
        'description_strategy': 'pass_through',
        'auto_write_enabled': True,
    },
}

# Abilita/disabilita mappatura fornitori fissi
MAPPATURA_FORNITORI_FISSI_ATTIVA = True

# Abilita/disabilita scrittura reale su Odoo (dry-run se False)
# IMPORTANTE: tenere False fino a quando non si è sicuri della logica
ODOO_WRITE_DRY_RUN = False


# ============================================================
# MAPPATURA_AUTOMEZZI — categoria dashboard "Automezzi"
# 7 fornitori noleggio veicoli (Leasys/Tecnoalt/UnipolRental/Athlon/
# Arval/ALD/LeasePlan). Pattern consume-POL multi-line con riscrittura
# completa POL libere su OdA-ledger annuali (Pattern A) o multi-OdA per
# veicolo (Tecnoalt 2026, Pattern B).
# Implementato 08/05/2026 in unico rilascio.
# ============================================================

# Conti contabili (id Odoo Ecotel verificati prod)
# Logica: <voce> x <classificazione> -> conto
CONTO_AUTOMEZZI = {
    ('locazione', 'POOL'):           398,    # 430210 — 100% deduc
    ('locazione', 'uso_promiscuo'):  399,    # 430220 — 70% deduc
    ('locazione', 'super_lusso'):   1119,    # 430225 — 20% deduc
    ('servizi',   'POOL'):           400,    # 430230 — 100% deduc
    ('servizi',   'uso_promiscuo'):  401,    # 430240 — 70% deduc
    ('servizi',   'super_lusso'):   1119,    # fallback su 430225
    ('tassa',     'POOL'):           161,    # 490300 — bollo 100%
    ('tassa',     'uso_promiscuo'): 1129,    # 490410 — bollo 70%
    ('tassa',     'super_lusso'):   1129,    # fallback
    # Spese di incasso bancario (Tecnoalt riga "SPESE DI INCASSO" 3.50€):
    # tutte vanno su conto oneri bancari indipendentemente da classificazione
    ('spese_incasso', 'POOL'):           134,    # 420410 — oneri bancari
    ('spese_incasso', 'uso_promiscuo'):  134,
    ('spese_incasso', 'super_lusso'):    134,
}

# Tax_id per fornitore + (voce, classificazione)
# Pattern dedotto da analisi cross-tab Q1 2026:
#   Leasys/ALD/Arval/UnipolRental: tax 6 (22% G — locazione operativa NLT)
#   Tecnoalt: tax 11 (22% S — noleggio attrezzature speciali)
#   Athlon: tax 11 standard, tax 73 (IVA indetraibile 60%) per super_lusso
#   Bollo/tasse: tax 47 (N1 escluse) per tutti
TAX_AUTOMEZZI = {
    'IT06714021000': {  # Leasys
        ('locazione',     '*'): 6,
        ('servizi',       '*'): 6,
        ('tassa',         '*'): 47,
        ('spese_incasso', '*'): 6,
    },
    'IT01924961004': {  # ALD Automotive
        ('locazione',     '*'): 6,
        ('servizi',       '*'): 6,
        ('tassa',         '*'): 47,
        ('spese_incasso', '*'): 6,
    },
    'IT04911190488': {  # Arval
        ('locazione',     '*'): 6,
        ('servizi',       '*'): 6,
        ('tassa',         '*'): 47,
        ('spese_incasso', '*'): 6,
    },
    'IT03740811207': {  # UnipolRental
        ('locazione',     '*'): 6,
        ('servizi',       '*'): 6,
        ('tassa',         '*'): 47,
        ('spese_incasso', '*'): 6,
    },
    'IT05580391000': {  # Tecnoalt — noleggio attrezzature
        ('locazione',     '*'): 11,
        ('servizi',       '*'): 11,
        ('tassa',         '*'): 47,
        ('spese_incasso', '*'): 11,    # tax 22% S anche su spese
    },
    'IT10641441000': {  # Athlon — leasing
        ('locazione', 'POOL'):          11,
        ('locazione', 'uso_promiscuo'): 11,
        ('locazione', 'super_lusso'):   73,    # IVA indetraibile 60%
        ('servizi',   'POOL'):          11,
        ('servizi',   'uso_promiscuo'): 11,
        ('servizi',   'super_lusso'):   73,
        ('tassa',     '*'):             47,
        ('spese_incasso', '*'):         11,
    },
    'IT02615080963': {  # LeasePlan — assumiamo come Leasys (NLT)
        ('locazione',     '*'): 6,
        ('servizi',       '*'): 6,
        ('tassa',         '*'): 47,
        ('spese_incasso', '*'): 6,
    },
}

# Mappatura principale per i 7 fornitori automezzi.
# Stato OdA verificato 08/05/2026 (vedi output/report_oda_automezzi_*.xlsx)
MAPPATURA_AUTOMEZZI = {
    'IT06714021000': {  # Leasys Italia S.p.A.
        'nome': 'Leasys Italia S.p.A.',
        'partner_id': 1638,
        'oda_fisso': 'P03021',     # 31 POL libere (~1 mese di copertura)
        'oda_oneri': 'P03555',     # SATURO, solo storico
        'multi_contratto': False,
        'description_strategy': 'leasys',
        'auto_write_enabled': True,
        'journal_id': 2,
        'company_id': 1,
    },
    'IT05580391000': {  # Tecnoalt S.r.l. — multi-OdA per veicolo
        'nome': 'Tecnoalt S.r.l.',
        'partner_id': 1305,
        'multi_contratto': True,
        # Override classificazione: tutti i veicoli/attrezzature Tecnoalt
        # sono POOL aziendale (van/gru/attrezzature lavorative). Se la targa
        # non è nel Parco Auto (es. GP220XA gru, attrezzature non standard),
        # usa POOL come default invece del fallback uso_promiscuo generico.
        'classificazione_default': 'POOL',
        'contratti': {
            # numero_contratto -> OdA target (verificati 09/05/2026)
            '693944':  {'oda_fisso': 'P04652', 'targa': 'GL999BT'},
            '817236':  {'oda_fisso': 'P04639', 'targa': 'GP642XH'},
            '839810':  {'oda_fisso': 'P04655', 'targa': 'GT453FN'},
            '822738':  {'oda_fisso': 'P04617', 'targa': 'GP454SF'},
            '642645':  {'oda_fisso': 'P04661', 'targa': 'GJ531GR'},
            '814785':  {'oda_fisso': 'P02976', 'targa': 'GP220XA'},  # GRU RETROCABINA EFFER
            # GK207XR (no num contratto) -> P04664 default
        },
        'oda_default': 'P04664',
        'description_strategy': 'tecnoalt',
        'auto_write_enabled': True,
        'journal_id': 2,
        'company_id': 1,
    },
    'IT03740811207': {  # UnipolRental S.p.A.
        'nome': 'UnipolRental S.p.A.',
        'partner_id': 1937,
        'multi_contratto': True,
        'contratti': {
            # Numero contratto estratto da regex "Contr. n.NNN" sulla desc XML
            '522375-3': {'oda_fisso': 'P03363'},
            '532705':   {'oda_fisso': 'P03365'},
            '1232866':  {'oda_fisso': 'P03495'},
            '1315327':  {'oda_fisso': 'P03495'},
            '1356145':  {'oda_fisso': 'P03495'},
            '1356154':  {'oda_fisso': 'P03495'},
            '1314450':  {'oda_fisso': 'P03363'},
        },
        'oda_default': 'P03495',     # OdA principale Ecotel multi-veicolo
        'description_strategy': 'unipol',
        'auto_write_enabled': True,
        'journal_id': 2,
        'company_id': 1,
    },
    'IT10641441000': {  # Athlon Car Lease Italy S.r.l.
        'nome': 'Athlon Car Lease Italy S.r.l.',
        'partner_id': 1984,
        'oda_fisso': 'P04797',       # nuovo 27/04/2026, POL HD446BE (Mercedes GLE)
        'oda_storico': 'P03798',     # POL GW950EK (BMW X4 Bruno Agostino)
        'multi_contratto': False,
        # Athlon ha 1 OdA per veicolo (P03798=GW950EK, P04797=HD446BE),
        # quindi fatture multi-veicolo spanano più OdA. Flag attiva l'auto-discovery
        # per targa cross-OdA anche fuori dal ramo multi_contratto.
        'multi_oda_per_targa': True,
        # POL pre-pianificate da Acquisti hanno già name semantico
        # ("Noleggio Mercedes-Benz GLE Coupé\nTarga: HD446BE\nPeriodo: ...").
        # Preservare il name evita di sovrascrivere con la desc XML muta
        # ("Fatturazione contratto").
        'keep_pol_name': True,
        # Se per (targa, periodo) non esiste POL libera, creiamo nuove POL
        # al volo + opzionalmente riempiamo i buchi mancanti fino alla prossima
        # POL pre-pianificata (es. Maggio + Giugno HD446BE).
        'auto_create_missing_pol': True,
        'fill_pol_gap_until_next': True,
        'description_strategy': 'athlon',
        'auto_write_enabled': True,
        'journal_id': 2,
        'company_id': 1,
    },
    'IT04911190488': {  # Arval Service Lease Italia
        'nome': 'Arval Service Lease Italia S.p.A.',
        'partner_id': 1287,
        'oda_fisso': 'P03405',
        'multi_contratto': False,
        'description_strategy': 'arval',
        'auto_write_enabled': True,
        'journal_id': 2,
        'company_id': 1,
    },
    'IT01924961004': {  # ALD Automotive Italia S.r.l.
        'nome': 'ALD Automotive Italia S.r.l.',
        'partner_id': 1000,
        'oda_fisso': 'P03053',
        'multi_contratto': False,
        'description_strategy': 'ald',
        'auto_write_enabled': True,
        'journal_id': 2,
        'company_id': 1,
    },
    'IT02615080963': {  # LeasePlan Italia S.p.A.
        'nome': 'LeasePlan Italia S.p.A.',
        'partner_id': 1415,
        'oda_fisso': None,           # nessun OdA attivo, da censire
        'multi_contratto': False,
        'description_strategy': 'leaseplan',
        'auto_write_enabled': False, # disattivo finché non c'è OdA
        'journal_id': 2,
        'company_id': 1,
    },
}

AUTOMEZZI_VATS = set(MAPPATURA_AUTOMEZZI.keys())
MAPPATURA_AUTOMEZZI_ATTIVA = True


# ============================================================
# HELPER: risoluzione mapping multi-contratto
# ============================================================

def resolve_mapping_entry(mapping_entry, xml_data):
    """
    Per fornitori multi-contratto (es. Telecom), risolve quale sub-entry
    (contratto → OdA) corrisponde alla fattura in base a IdDocumento/DatiContratto.

    Ritorna un dict "flat" con i campi del parent + quelli del contratto matchato,
    oppure None se nessun contratto corrisponde.

    Per fornitori normali (non multi_contratto) ritorna mapping_entry così com'è.
    """
    if not mapping_entry:
        return None

    if not mapping_entry.get('multi_contratto'):
        return mapping_entry

    contratti_config = mapping_entry.get('contratti', {})
    if not contratti_config or not xml_data:
        return None

    # Cerco match tra i contratti dell'XML e quelli configurati
    # Fonti: DatiContratto, DatiOrdineAcquisto (valori grezzi), DatiRicezione,
    # RiferimentoAmministrazione del CedentePrestatore (Wind Tre).
    xml_refs = set()
    for ref in getattr(xml_data, 'contratto_riferimenti', []):
        xml_refs.add(ref.strip())
    for ref in getattr(xml_data, 'oda_valori_grezzi', []):
        xml_refs.add(ref.strip())
    for ref in getattr(xml_data, 'ricezione_riferimenti', []):
        xml_refs.add(ref.strip())
    rif_amm = getattr(xml_data, 'cedente_riferimento_amministrazione', '') or ''
    if rif_amm.strip():
        xml_refs.add(rif_amm.strip())
    for pod in getattr(xml_data, 'pod_riferimenti', []) or []:
        xml_refs.add(pod.strip())
    # Codice cliente (Telepass-network: routing via Causale "Codice cliente: NNN")
    cc = getattr(xml_data, 'codice_cliente', None)
    if cc:
        xml_refs.add(cc.strip())

    matched_contratto = None
    matched_key = None
    for cfg_key in contratti_config:
        if cfg_key in xml_refs:
            matched_contratto = contratti_config[cfg_key]
            matched_key = cfg_key
            break

    if not matched_contratto:
        return None

    # Merge: parent + contratto (contratto sovrascrive)
    resolved = {}
    for k, v in mapping_entry.items():
        if k != 'contratti' and k != 'multi_contratto':
            resolved[k] = v
    resolved.update(matched_contratto)
    resolved['contratto_id'] = matched_key
    return resolved
