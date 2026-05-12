"""
Genera la guida PDF "Gestione file Apparati Parco Auto" da consegnare
all'ufficio parco/contabilità.

Uso:
    python scripts/genera_guida_apparati_pdf.py
    python scripts/genera_guida_apparati_pdf.py --output <path.pdf>
"""
import argparse
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                  TableStyle, PageBreak, ListFlowable, ListItem)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "Guida_Apparati_Parco.pdf"


def build_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='H1Custom', parent=styles['Heading1'],
                                fontSize=18, spaceAfter=12, textColor=colors.HexColor('#1a3a5c')))
    styles.add(ParagraphStyle(name='H2Custom', parent=styles['Heading2'],
                                fontSize=13, spaceBefore=12, spaceAfter=6,
                                textColor=colors.HexColor('#1a3a5c')))
    styles.add(ParagraphStyle(name='H3Custom', parent=styles['Heading3'],
                                fontSize=11, spaceBefore=8, spaceAfter=4,
                                textColor=colors.HexColor('#444')))
    styles.add(ParagraphStyle(name='BodyJust', parent=styles['BodyText'],
                                alignment=4, leading=14, spaceAfter=6))
    styles.add(ParagraphStyle(name='Mono', parent=styles['BodyText'],
                                fontName='Courier', fontSize=9, leading=11))
    styles.add(ParagraphStyle(name='Warn', parent=styles['BodyText'],
                                backColor=colors.HexColor('#fff4e0'),
                                borderPadding=6, leading=14,
                                textColor=colors.HexColor('#7a4500')))
    return styles


def build_story(styles):
    s = []
    H1, H2, H3 = styles['H1Custom'], styles['H2Custom'], styles['H3Custom']
    P = styles['BodyJust']
    W = styles['Warn']

    # Cover
    s.append(Paragraph("Guida gestione file <b>Apparati Parco Auto</b>", H1))
    s.append(Paragraph(
        f"Documento per ufficio Parco/Contabilità — Ecotel Italia<br/>"
        f"Versione del {date.today().strftime('%d/%m/%Y')}", styles['BodyText']))
    s.append(Spacer(1, 0.4*cm))

    # 1 — Scopo
    s.append(Paragraph("1. Scopo del file", H2))
    s.append(Paragraph(
        "Il file <b>Apparati completo TOT.xlsx</b> è la sorgente di verità "
        "per la classificazione fiscale dei dispositivi del parco auto "
        "Ecotel (telepass, viacard, ecc.) usati su autostrade, parcheggi e "
        "carburanti. L'agent di fatturazione legge questo file per registrare "
        "automaticamente le fatture passive con lo split corretto fra "
        "<b>furgoni</b> (deducibili 100%) e <b>uso promiscuo</b> (deducibili "
        "70%).", P))
    s.append(Paragraph(
        "Senza questo file aggiornato, la registrazione richiede intervento "
        "manuale del contabile per ogni fattura.", P))

    # 2 — Struttura
    s.append(Paragraph("2. Struttura del file", H2))
    s.append(Paragraph(
        "Il file ha un foglio <b>Totale</b> con una riga per ogni "
        "apparato. Le colonne richieste sono:", P))

    cols = [
        ["Colonna", "Obbligatoria", "Descrizione"],
        ["Stato", "Sì", "MAPPATO / CENSITO / DISMESSO / GUASTA (vedi §3)"],
        ["Fornitore(i)", "No", "Es. Autostrade, Apcoa — solo informativo"],
        ["Tipo apparato", "Sì", "TELEPASS / VIACARD"],
        ["Codice apparato", "Sì", "Codice del PDF (es. 286611587 o 3.855921.19)"],
        ["Codice cliente (cc)", "Sì", "Numero contratto cliente (es. 261713569)"],
        ["TARGA", "Quando nota", "Targa formato XX000XX (lasciare 'Pool' se non assegnata)"],
        ["VEICOLO descrizione", "No", "Modello veicolo (lasciare 'Pool' per carte di flotta)"],
        ["CLASSIFICAZIONE", "Sì", "<b>furgoni</b> oppure <b>uso_promiscuo</b>"],
        ["Note", "No", "Es. 'guasta', 'in attesa sostituzione'"],
    ]
    t = Table(cols, colWidths=[3.5*cm, 2.2*cm, 8.8*cm])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a3a5c')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    s.append(t)
    s.append(Spacer(1, 0.3*cm))

    # 3 — Stati
    s.append(Paragraph("3. Stati ammessi (colonna <i>Stato</i>)", H2))
    stati = [
        ["Stato", "Significato", "Apparato resta nel file?"],
        ["MAPPATO", "Apparato attivo, info complete (targa+veicolo specifici)", "Sì"],
        ["CENSITO", "Apparato attivo di flotta/pool (targa generica 'Pool')", "Sì"],
        ["DISMESSO", "Apparato restituito o disattivato dal contratto", "<b>Sì — non eliminare</b>"],
        ["GUASTA", "Apparato non più operativo per guasto fisico", "<b>Sì — non eliminare</b>"],
    ]
    t2 = Table(stati, colWidths=[2.5*cm, 8*cm, 4*cm])
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a3a5c')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    s.append(t2)
    s.append(Spacer(1, 0.3*cm))

    s.append(Paragraph(
        "<b>Perché DISMESSO/GUASTA restano nel file?</b><br/>"
        "Le fatture passive arrivano spesso con qualche mese di ritardo. "
        "Una fattura di gennaio che cita un apparato dismesso a maggio deve "
        "comunque trovare l'apparato nel file per essere classificata "
        "correttamente. L'agent considera attivi solo MAPPATO e CENSITO ma "
        "usa anche gli altri due stati per le fatture pregresse.", W))

    # 4 — Regole d'oro
    s.append(Paragraph("4. Regole d'oro", H2))
    rules = [
        "<b>Mai eliminare righe</b>: per dismissioni o guasti, cambiare solo lo stato.",
        "<b>Mai duplicare il codice apparato</b> all'interno dello stesso codice cliente (cc): "
        "se l'apparato è stato sostituito, dismetti il vecchio e aggiungi una nuova riga col nuovo codice.",
        "<b>Compilare CLASSIFICAZIONE</b> con esattamente <i>furgoni</i> o <i>uso_promiscuo</i>: "
        "qualsiasi altro valore viene scartato dall'agent.",
        "<b>TARGA in formato standard</b> (XX000XX in maiuscolo, es. GP642XH). "
        "Per carte di flotta non assegnate scrivere <i>Pool</i>.",
        "<b>Salvare in formato .xlsx</b>: l'agent non legge .xls vecchi né .csv.",
        "<b>Una sola versione attiva</b>: il file lavorativo è "
        "<i>Apparati completo TOT.xlsx</i>. Rinominare le copie storiche con suffisso data.",
    ]
    s.append(ListFlowable(
        [ListItem(Paragraph(r, P), leftIndent=15, value='circle') for r in rules],
        bulletType='bullet'))

    s.append(PageBreak())

    # 5 — Workflow
    s.append(Paragraph("5. Workflow operativo", H2))

    s.append(Paragraph("5.1 Quando aggiornare il file", H3))
    s.append(Paragraph(
        "L'agent vi segnalerà via email/dashboard ogni volta che processando "
        "una fattura trova un apparato non presente nel file (stato implicito "
        "DA CENSIRE). In tal caso:", P))
    sub = [
        "Aprire <i>Apparati completo TOT.xlsx</i> dalla cartella condivisa.",
        "Aggiungere una nuova riga in fondo con stato <b>CENSITO</b>.",
        "Compilare almeno: Tipo apparato, Codice apparato, cc, Targa (o 'Pool'), Classificazione.",
        "Salvare e consegnare il file aggiornato (vedi §5.4).",
    ]
    s.append(ListFlowable(
        [ListItem(Paragraph(r, P), leftIndent=15, value='1') for r in sub],
        bulletType='1', start='1'))

    s.append(Paragraph("5.2 Quando un apparato si guasta", H3))
    s.append(Paragraph(
        "Trovare la riga e cambiare la colonna <i>Stato</i> da MAPPATO/CENSITO "
        "a <b>GUASTA</b>. Aggiungere nelle Note la data di guasto se "
        "disponibile. <b>Non eliminare la riga.</b>", P))

    s.append(Paragraph("5.3 Quando un apparato viene dismesso", H3))
    s.append(Paragraph(
        "Cambiare lo stato a <b>DISMESSO</b> e aggiungere nelle Note la data "
        "di restituzione. Se l'apparato è stato sostituito da un nuovo "
        "codice, aggiungere una nuova riga MAPPATO per il nuovo codice "
        "(stesso veicolo, stesso cc).", P))

    s.append(Paragraph("5.4 Consegna del file aggiornato", H3))
    s.append(Paragraph(
        "Salvare il file in formato .xlsx mantenendo il nome "
        "<i>Apparati completo TOT.xlsx</i> e collocarlo nella cartella "
        "condivisa di riferimento. L'agent rileggerà la nuova versione "
        "alla prossima esecuzione.", P))

    s.append(Paragraph("6. Cosa NON fare", H2))
    bads = [
        "Eliminare righe di apparati dismessi o guasti.",
        "Modificare l'ordine delle colonne nel foglio.",
        "Inserire formule complesse nelle celle (l'agent legge solo i valori).",
        "Lasciare stati ambigui ('da verificare', 'in attesa', 'ok'): usare solo i 4 ammessi.",
        "Modificare la classificazione di un apparato senza concordarla con la contabilità.",
    ]
    s.append(ListFlowable(
        [ListItem(Paragraph(r, P), leftIndent=15, value='circle') for r in bads],
        bulletType='bullet'))

    # 7 — FAQ
    s.append(Paragraph("7. FAQ", H2))

    faq = [
        ("Cosa significa <i>uso_promiscuo</i> vs <i>furgoni</i>?",
          "<b>furgoni</b>: veicoli aziendali a pieno utilizzo lavorativo "
          "(deducibilità IVA 100%). <b>uso_promiscuo</b>: veicoli con uso anche "
          "personale del dipendente (deducibilità IVA 40%). La regola fiscale "
          "viene applicata automaticamente dall'agent."),
        ("Lo stesso apparato può essere su più cc?",
          "Sì, in casi rari (es. viacard pool trasferita tra contratti). "
          "Aggiungere una nuova riga col medesimo codice ma cc diverso. "
          "L'agent gestisce il caso e segnala un avviso informativo."),
        ("Cosa fare se non si conosce ancora la targa di un nuovo apparato?",
          "Compilare la riga con stato <b>CENSITO</b> e mettere <i>Pool</i> "
          "in TARGA e VEICOLO. Aggiornare in seguito quando l'assegnazione "
          "è certa."),
        ("Cosa fare se la classificazione (furgoni / uso_promiscuo) cambia?",
          "Modificare il valore nella stessa riga. L'agent applicherà la nuova "
          "classificazione alle fatture future. Non viene riprocessato lo storico."),
    ]
    for q, a in faq:
        s.append(Paragraph(f"<b>{q}</b>", styles['BodyText']))
        s.append(Paragraph(a, P))
        s.append(Spacer(1, 0.15*cm))

    s.append(Spacer(1, 0.5*cm))
    s.append(Paragraph(
        "<i>Per chiarimenti tecnici contattare l'amministratore dell'agent "
        "di fatturazione passiva (Luca Ranalletta).</i>", styles['BodyText']))

    return s


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                          help=f'Path PDF output (default: {DEFAULT_OUTPUT})')
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    styles = build_styles()
    doc = SimpleDocTemplate(
        str(args.output), pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
        title='Guida gestione file Apparati Parco Auto',
        author='Ecotel Italia — Agent Fatturazione Passiva',
    )
    story = build_story(styles)
    doc.build(story)
    print(f"Generato: {args.output}")
    print(f"Dimensione: {args.output.stat().st_size} bytes")


if __name__ == '__main__':
    main()
