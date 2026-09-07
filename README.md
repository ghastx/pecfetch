# pecfetch

Scarica in **sola lettura** le caselle PEC di uno studio, smonta le buste di
trasporto e scrive il contenuto reale come dati strutturati in una cartella
locale, con allegati, testo estratto dagli allegati e sorgente originale.

Non classifica, non inoltra, non scrive nulla sulle caselle e non chiama modelli
linguistici. Produce solo dati puliti; cosa farne è di un altro componente.

---

## Architettura in mezza pagina

```
        IMAP (EXAMINE + BODY.PEEK)          cartella di output
                  |                          |
   imapclient ----+                    +---- output    coda/<messaggio>/ + indice/*.jsonl
        |                              |               esiti/  (del consumatore)
   pipeline ------ pec (parsing) ------+---- extract   testo di PDF/office/p7m, OCR ita
        |                              |         |
   state (SQLite, locale)              |      safety   tipi attivi, limiti sugli archivi
                                       +---- archive   SQLite + FTS5, memoria storica
```

* **`imapclient`** — `imaplib` della standard library, ma solo `EXAMINE` e
  `BODY.PEEK[]`. Nessun `SELECT`, `STORE`, `COPY`, `EXPUNGE`: Thunderbird
  continua a lavorare sulle stesse caselle senza accorgersi di niente. Un test
  ispeziona il sorgente della classe per impedire regressioni su questo punto.
* **`state`** — SQLite locale alla VM. È **l'unica** verità su cosa è stato
  scaricato: cursore `(UIDVALIDITY, ultimo UID)` per casella, impronta SHA-256
  del contenuto per la deduplica, avanzamento a fasi per ogni messaggio.
* **`pec`** — riconoscimento e smontaggio delle buste: `posta-certificata`,
  `busta di anomalia`, le sette ricevute, generico. Priorità a `daticert.xml`,
  ripiego sugli header `X-*`, ultimo ripiego sull'oggetto normalizzato.
* **`output`** — scrittura dei dati: montaggio in `.tmp-pecfetch`,
  `rename` atomico dentro `coda/`, riga JSONL in append nell'indice del giorno.
* **`extract`** — testo degli allegati: PDF nativo (pdftotext/pypdf/pdfminer),
  ripiego OCR in italiano per gli atti scansionati, docx/xlsx/pptx/odf e archivi
  con la sola standard library, sbustamento CAdES `.p7m`. Ogni dipendenza è
  opzionale e caricata lazy: se manca, il messaggio esce lo stesso con l'esito
  annotato.
* **`safety`** — le regole su cosa si apre e cosa si scrive: tipi attivi, limiti
  sugli archivi, XML senza dichiarazioni di entità. Vedi più sotto.
* **`archive`** — SQLite con FTS5, gli stessi record dell'indice, per la
  ricerca retrospettiva per cliente, mittente, periodo e testo.
* **`pipeline`** — orchestrazione, gestione degli errori per casella,
  riepilogo finale.

### L'ordine che garantisce "nessuna perdita"

Per ogni messaggio, in questo ordine e non in un altro:

1. scarico con `BODY.PEEK` (nessun flag toccato);
2. impronta del contenuto e controllo deduplica;
3. **registro il messaggio nello stato** (prima di scrivere qualsiasi file);
4. monto la cartella in `.tmp-pecfetch` e la sposto in `coda/` con un `rename`;
5. riga d'indice, poi archivio: ogni fase confermata separatamente;
6. **solo ora avanzo il cursore**.

Un'interruzione fra il 3 e il 6 fa riscaricare il messaggio alla prossima
esecuzione: nel dubbio si riscarica, non si salta. La riscrittura è idempotente
perché il nome della cartella deriva dal contenuto, quindi due esecuzioni
consecutive non producono duplicati e un'interruzione non lascia né stato
incoerente né file parziali visibili al consumatore.

### UIDVALIDITY e deduplica

Se il gestore ricrea la casella, la `UIDVALIDITY` cambia e gli UID memorizzati
diventano carta straccia: il cursore riparte da zero e la risincronizzazione
avviene per data (dall'ultimo messaggio visto, meno `resync_overlap_days`). A
quel punto è l'impronta del contenuto a evitare di riemettere quello che era
già uscito. La deduplica è **per casella**: la stessa PEC ricevuta da due
clienti diversi resta giustamente due messaggi.

---

## Contratto di output

La radice è organizzata su due livelli, perché il consumatore lavora in due
passate: prima scorre tutto per capire cosa merita attenzione, poi legge a
fondo i pochi messaggi selezionati.

```
indice/AAAA-MM-GG.jsonl    un record JSON per riga, un messaggio per record
coda/<messaggio>/          messaggio.json  metadati completi
                           corpo.txt       corpo, sempre testo, mai dentro il JSON
                           allegati/<file> allegato, nome sanificato
                           allegati/<file>.txt  testo estratto
                           busta.eml       sorgente originale integrale
                           postacert.eml   messaggio interno alla busta
                           daticert.xml    metadati certificati del gestore
esiti/                     riservata al consumatore, append-only, pecfetch non tocca
.tmp-pecfetch/             area di montaggio, da ignorare
CONTRATTO.md               questa stessa descrizione, scritta nella radice
```

Un record d'indice porta: casella ricevente ed etichetta leggibile, cliente,
tipo e se è certificato, date (certificata, invio, ricezione), mittente reale
con dominio, destinatari, oggetto decodificato, elenco allegati con nome tipo
dimensione ed esito dell'estrazione, `message_id`, identificativo PEC, gestore,
riferimento al messaggio originale per le ricevute, e i puntatori al contenuto
completo. Sta sotto i 2 KB: mille messaggi si leggono in un colpo solo.

Le **ricevute positive** (accettazione, presa in carico, avvenuta consegna) non
compaiono in output: sono registrate nello stato locale (`pecfetch receipts`).
Le **ricevute negative** escono, collegate al messaggio originale via
`ricevuta.riferimento_message_id`.

Il consumatore può spostare o cancellare quello che ha processato: pecfetch non
si stupisce se qualcosa sparisce e non riscrive ciò che è già uscito.

---

## Uso

```sh
pecfetch check --login          # configurazione, strumenti, collegamenti
pecfetch init                   # fissa la posizione attuale, non scarica nulla
pecfetch init --lookback-days 7 # ...oppure parti da 7 giorni fa
pecfetch run                    # uso normale (systemd timer)
pecfetch run --dry-run          # cosa scaricherebbe
pecfetch backfill --since 2024-01-01 -a rossi   # archivio storico, esplicito
pecfetch status                 # cursori, ultime esecuzioni, errori
pecfetch search "avviso accertamento" --client ROSSI --since 2024-01-01
pecfetch receipts               # ricevute positive registrate
pecfetch parse busta.eml        # diagnostica su un .eml locale, senza IMAP
```

Codici di uscita: `0` tutto bene, `2` completato con caselle in errore, `1`
errore fatale, `3` un'altra esecuzione era già in corso.

Un lock su file (`flock`) impedisce esecuzioni sovrapposte, quindi il timer può
scattare anche mentre un run lungo è ancora in corso.

Installazione e configurazione: [`deploy/INSTALL.md`](deploy/INSTALL.md) e
[`config/pecfetch.example.toml`](config/pecfetch.example.toml).

---

## Scelte prese dove le indicazioni lasciavano margine

* **Solo standard library per il nucleo.** IMAP, MIME, SQLite, XML, ZIP: tutto
  stdlib. Le dipendenze esterne servono solo all'estrazione del testo e sono
  facoltative. Su una VM di servizio che deve girare per anni è una scelta di
  manutenzione, non di gusto.
* **Indice in JSONL giornaliero.** Un record per riga, indipendente, in append
  senza lock; un giorno intero si legge in blocco. Niente file monolitico con
  dentro anche i contenuti.
* **L'archivio storico è un SQLite accanto allo stato**, configurabile con
  `[archive].path`.
* **Sbustamento `.p7m`.** Non era nel testo, ma sulle PEC di lavoro gli atti
  degli enti arrivano quasi sempre firmati CAdES: senza sbustare, l'estrazione
  del testo fallisce proprio sui documenti che contano.
* **Nome della cartella leggibile**: `data_casella_oggetto_id`. Le cartelle le
  aprono anche esseri umani.
* **Valvola di sicurezza sui messaggi problematici**: dopo tre tentativi
  falliti sullo stesso messaggio, viene scritto comunque senza estrazione del
  testo, con la nota nei metadati. Meglio un messaggio incompleto in coda che
  una casella bloccata per sempre su un PDF malato.

## Allegati ostili

`extract` apre per mestiere file che arrivano da mittenti sconosciuti: sulle
caselle PEC girano campagne ricorrenti di finte fatture con allegato compresso,
spedite da caselle certificate compromesse. Le regole stanno in `safety.py`.

* **Non si esegue niente, mai.** Niente LibreOffice, niente interpreti: i
  formati office si smontano come ZIP + XML, quindi nessuna macro viene
  attivata. I `.docm`/`.xlsm` si leggono, ma escono marcati `contenuto_attivo`.
* **Eseguibili e script non vengono mai scritti su disco.** Vale per gli
  allegati diretti e per le voci degli archivi. Restano censiti nei metadati
  (nome, tipo, dimensione, sha256) e integri dentro `busta.eml`. Vale il
  contenuto, non l'estensione: un `fattura.pdf` che comincia per `MZ` è un
  eseguibile travestito, e viene trattato come tale.
* **Gli archivi si aprono con limiti espliciti** su rapporto di espansione,
  byte totali, numero di voci, annidamento e dimensione della singola voce
  (`[extraction.archive]`). I contatori sono condivisi da tutto l'albero, non
  per livello. Le voci si leggono a blocchi, senza fidarsi della dimensione
  dichiarata nell'header. Al superamento di un limite la lettura si ferma e
  `limite_superato` dice quale.
* **Il percorso dichiarato dentro un archivio non è un percorso.** Le voci
  finiscono in `allegati/<archivio>.d/NNN_<nome>` con nomi generati da pecfetch;
  il percorso originale resta nei metadati come dato. `../../etc/passwd` diventa
  `001_passwd`.
* **`rar`, `7z` e le immagini disco non si aprono**: sono i formati che il
  malspam usa per evadere i controlli, e non aprirli è la risposta giusta.
  Escono come `unsupported_archive`.
* **XML senza DTD.** `daticert.xml` e i formati office si parsano rifiutando le
  dichiarazioni di entità: la protezione di expat contro l'amplificazione
  dipende dalla versione installata e non ci si appoggia. Un daticert con DTD
  ricade sul ripiego a regex e i metadati certificati si recuperano lo stesso.

Niente di tutto questo può far mancare un messaggio: un allegato che non si è
potuto trattare produce un messaggio **annotato**, non un messaggio assente.

**Limite dichiarato**: non c'è confinamento di processo. Gli strumenti esterni
(`pdftotext`, `pdftoppm`, `tesseract`, `openssl`) girano con un timeout ma senza
limiti di memoria e senza sandbox. Un PDF costruito per sfruttare un bug di
memoria in poppler eseguirebbe codice come utente `pecfetch`, dentro il
perimetro dell'unit systemd. È l'attacco mirato che il progetto dichiara fuori
scopo; se un domani lo si vuole coprire, il punto d'innesto è `extract._run`.

## Sviluppo

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,extract]'
pytest
```

I test coprono il parsing delle varie forme di busta, la decodifica difensiva,
la logica di stato (UIDVALIDITY, deduplica, ordine delle fasi), il contratto di
output e il comportamento del pipeline in caso di errore. Nessun test richiede
una connessione IMAP: le caselle sono finte e vivono in memoria.
