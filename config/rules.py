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
MATCH_PARZIALE_MAX_RIGHE = 12

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
}

# Abilita/disabilita mappatura fornitori fissi
MAPPATURA_FORNITORI_FISSI_ATTIVA = True

# Abilita/disabilita scrittura reale su Odoo (dry-run se False)
# IMPORTANTE: tenere False fino a quando non si è sicuri della logica
ODOO_WRITE_DRY_RUN = False


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
