# Odoo Invoice Matching Agent

Agent esterno per l'analisi e il matching automatico delle fatture passive elettroniche
con ordini di acquisto in Odoo 14 Enterprise.

## Cosa fa

Ogni esecuzione (schedulata o on-demand) l'agent:

1. Si connette alla tua istanza Odoo via XML-RPC in **sola lettura**
2. Recupera le fatture fornitore in stato bozza/da verificare
3. Per ciascuna fattura esegue il matching con l'ordine di acquisto collegato
4. Classifica ogni fattura in una delle categorie:
   - **AUTO_VALIDABILE**: match OK, differenze entro tolleranza
   - **TRASPORTO_OK**: righe extra riconosciute come spese di trasporto
   - **DA_VERIFICARE**: differenze oltre tolleranza, richiede intervento umano
   - **ODA_MANCANTE**: nessun OdA collegato o riferimento non trovato
   - **ANOMALIA**: errori di elaborazione, fattura malformata
5. Genera report HTML, Excel e dashboard riepilogativa
6. **NON modifica nulla in Odoo** - tutte le azioni restano manuali

## Architettura

```
odoo_invoice_agent/
├── config/           # File di configurazione (credenziali, regole)
├── core/             # Logica di business
│   ├── odoo_client.py      # Connessione XML-RPC (sola lettura)
│   ├── matcher.py          # Logica di matching fattura/OdA
│   ├── classifier.py       # Classificazione fatture nelle 5 categorie
│   └── keyword_rules.py    # Riconoscimento righe trasporto/extra
├── reports/          # Generatori di report
│   ├── dashboard.py        # Dashboard HTML sintetica
│   ├── excel_report.py     # Report Excel dettagliato
│   └── exceptions.py       # Lista fatture da verificare
├── output/           # Destinazione report generati
├── logs/             # Log di esecuzione
└── run_agent.py      # Entry point principale
```

## Sicurezza

- Connessione Odoo con utente dedicato a **sola lettura**
- Credenziali in file `config/credentials.env` (mai in git)
- Log completo di ogni analisi per audit
- Zero scritture su database di produzione

## Come si usa

```bash
# Esecuzione on-demand
python run_agent.py

# Esecuzione con range date specifico
python run_agent.py --from 2026-04-01 --to 2026-04-18

# Solo dashboard senza report dettagliato
python run_agent.py --dashboard-only

# Schedulazione via cron (esempio: ogni notte alle 02:00)
0 2 * * * cd /path/to/agent && python run_agent.py >> logs/cron.log 2>&1
```

## Output

A fine esecuzione trovi in `output/YYYY-MM-DD/`:
- `dashboard.html` - Vista d'insieme con KPI
- `report_dettagliato.xlsx` - Tutte le fatture con classificazione
- `da_verificare.xlsx` - Solo le eccezioni, ordinate per priorità
- `registrabili.xlsx` - Lista pronta per data entry in Odoo
