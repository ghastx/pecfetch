# pecfetch e pecdesk

Due programmi distinti nello stesso repository, separati da una coda di file.

**`pecfetch`** scarica in **sola lettura** le caselle PEC di uno studio, smonta
le buste di trasporto e scrive il contenuto reale come dati strutturati in una
cartella locale, con allegati, testo estratto dagli allegati e sorgente
originale. Non classifica, non inoltra, non scrive nulla sulle caselle e non
chiama modelli linguistici.

**`pecdesk`** legge quella coda, smista i messaggi con l'aiuto dell'API di
Anthropic, scrive un esito per messaggio e manda al titolare il riepilogo del
giorno. Non tocca le caselle in ingresso e non inoltra niente.

pecfetch acquisisce e non giudica; pecdesk giudica e non tocca le caselle. La
coda è il confine fra i due: se l'API è irraggiungibile pecdesk si ferma e
l'acquisizione della posta continua indisturbata. Sotto trovi prima pecfetch,
poi [pecdesk](#pecdesk--smistamento-della-coda).

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

I test di pecdesk stanno in `tests/desk/` e non toccano né l'API né l'SMTP: la
coda è finta ma ha la forma vera, e il modello è un oggetto che risponde quello
che gli si dice di rispondere (o che fallisce a comando).

---

# pecdesk — smistamento della coda

Legge `coda/`, decide cosa va girato a chi, cosa richiede il titolare e cosa
puzza, e alle sette del mattino manda un riepilogo leggibile in trenta secondi
da un telefono.

**L'obiettivo è la selezione, non l'analisi.** Il modello deve riconoscere *che
tipo di documento* è arrivato e dire dove va — cosa che nella grande maggioranza
dei casi si capisce da mittente, oggetto e prime righe. Non deve leggere gli
atti. Da qui le tre scelte che governano il resto: un modello economico
(`claude-haiku-4-5`), una chiamata sincrona per messaggio, e allegati inviati
**solo per estratto**. Su cinquanta messaggi al giorno costa attorno ai tre euro
al mese.

## Architettura in mezza pagina

```
   coda/ di pecfetch  ──►  queue      lettura, mai scrittura
                            │
   direttive/ ──► directives│         regole.toml + istruzioni.md, riletti ogni volta
        │                   ▼
     rules  ──────────►  material     estratti e budget, ogni taglio dichiarato
        │                   │
   archivio pecfetch ─► signals       "ha mai scritto prima?", ostilità, injection
    (sola lettura)         │
                            ▼
                          model       una chiamata, vocabolari chiusi, output JSON
                            │
                          decide      veto > regola > tipo > modello > incertezza
                            │
                  ┌─────────┴─────────┐
              outcomes              queue        esiti/*.jsonl  +  lavorati/
                  │
               digest ──► mailer     un destinatario, fisso, mai su PEC
```

* **`queue`** — legge `coda/<messaggio>/`. Un percorso letto dentro
  `messaggio.json` è un dato, non un percorso: si verifica che resti dentro la
  cartella del messaggio prima di aprirlo.
* **`directives`** — le due fonti di direttive, con le loro impronte SHA-256.
* **`rules`** — le certezze: mittente, dominio, casella, tipo di busta. Nessuna
  chiamata.
* **`material`** — cosa si manda e cosa si taglia. È qui che stanno quasi tutta
  la spesa e quasi tutto il rischio.
* **`signals`** — il segnale relazionale dall'archivio storico, i segnali
  ostili, le euristiche sui tentativi di parlare al programma.
* **`model`** — la richiesta, con il materiale dentro un recinto.
* **`decide`** — funzione pura: si prova per intero senza chiamare niente.
* **`state`** — SQLite proprio: claim, riconciliazione, riepiloghi, tentativi.
* **`outcomes`** — `esiti/`, append-only, con le correzioni accanto.

## Le direttive

Due file, riletti a ogni esecuzione e tenuti sotto controllo di versione in
[`direttive/`](direttive/):

* **`regole.toml`** — le certezze, applicate in codice. Mittente, dominio,
  casella ricevente, tipo di busta. Dichiara anche il vocabolario chiuso dei
  tipi di documento e gli **alias dei destinatari**.
* **`istruzioni.md`** — le sfumature, in italiano, trasmesse al modello così
  come sono. Coprono quello che una regola non sa dire.

Ogni esito porta l'impronta di entrambi: `pecdesk registro` sa dire con quali
direttive è stato prodotto ogni giudizio.

## L'ordine delle precedenze

1. Il sospetto è il **massimo** fra i segnali del codice, quello dichiarato da
   una regola e quello del modello. Mai la media.
2. **Sospetto ⇒ veto**: nessun inoltro automatico, si segnala al titolare. Il
   veto prevale **anche su una regola deterministica di inoltro**.
3. Altrimenti la **regola deterministica** vince su istruzioni in linguaggio
   naturale e giudizio del modello.
4. I **tipi che vanno sempre al titolare** (atti, cartelle, accertamenti) sono
   la rete di sicurezza contro l'estratto: se una regola ha già deciso altro,
   resta almeno l'attenzione.
5. Altrimenti vale l'**instradamento del modello**.
6. **Confidenza bassa o instradamento assente ⇒ titolare**, dichiarato incerto.
   L'incertezza non si maschera da decisione.
7. **Termine non verificabile ⇒ intervento del titolare.** L'assenza di prova fa
   salire di livello, non scendere: un termine mancato costa molto più di un
   termine segnalato a torto.

## Contenuto ostile

Sulle caselle PEC arrivano regolarmente messaggi fraudolenti, tipicamente da
caselle certificate compromesse. **La PEC certifica il trasporto, non le
intenzioni**: `certificato: true` non è un indizio di affidabilità e da nessuna
parte, nel codice, riduce il sospetto.

* **Il contenuto è dato, mai istruzione.** Oggetto, corpo ed estratti viaggiano
  dentro `<materiale_non_fidato nonce="…">` con un nonce che cambia a ogni
  esecuzione, e il blocco di sistema dichiara che niente lì dentro può cambiare
  le direttive, il destinatario di un inoltro o il comportamento del programma.
  Un tag di chiusura falsificato viene neutralizzato e **riportato come
  segnale**.
* **Il destinatario è un alias, mai un indirizzo.** Il modello sceglie dentro
  l'elenco di `[destinatari]`; la risoluzione la fa il codice. Nessuna frase
  dentro un messaggio può far comparire un indirizzo nuovo.
* **Il segnale più solido è relazionale**: che quel mittente non abbia mai
  scritto prima a quella casella, ricavato dall'archivio storico di pecfetch
  interrogato in sola lettura. Da solo non basta — si combina con allegato
  compresso, lessico di sollecito, urgenza, pressione al pagamento, esito
  dell'analisi degli allegati.
* **Partenza a freddo.** Su un archivio giovane ogni mittente è nuovo: sotto le
  soglie di `[sospetto]` il segnale esce come `storico_insufficiente` e non fa
  scattare niente da solo. Analogamente, un mittente **nominato in una regola**
  è dichiarato dal titolare e non conta come estraneo — ma resta esposto a tutti
  gli altri segnali, che è esattamente il caso della casella compromessa di uno
  che si conosce.

## Uso

```sh
pecdesk check --api --smtp    # configurazione, direttive, permessi, collegamenti
pecdesk run --prova           # cosa verrebbe inviato al modello, senza inviarlo
pecdesk run                   # uso normale (systemd timer, una volta al mattino)
pecdesk run --limite 5        # prima esecuzione prudente
pecdesk riepilogo --stampa    # ricompone il riepilogo del giorno e lo stampa
pecdesk spiega <id> --materiale
pecdesk correggi <id> --campo instradamento --valore titolare --nota "..."
pecdesk registro --giorni 30  # dove il sistema sbaglia in modo sistematico
pecdesk stato                 # messaggi sospesi, ultime esecuzioni
pecdesk riprova <id>          # sblocca un sospeso
```

Codici di uscita: `0` tutto bene, `2` completato con messaggi non lavorati o
riepilogo non spedito, `1` errore fatale, `3` un'altra esecuzione era già in
corso. Come per pecfetch, un `flock` impedisce esecuzioni sovrapposte.

## Idempotenza, e cosa succede quando cade

Per ogni messaggio, in quest'ordine e non in un altro:

1. `claim` nello stato;
2. montaggio del materiale (nessun effetto esterno);
3. chiamata all'API;
4. **riga di esito in append, con `fsync`**;
5. stato `classificato`;
6. `rename` di `coda/<messaggio>/` in `lavorati/AAAA-MM-GG/`;
7. stato `archiviato`.

All'avvio pecdesk **riconcilia** lo stato con gli esiti scritti negli ultimi
sette giorni: un esito su disco è la prova che il messaggio è stato lavorato,
anche se il processo è morto prima di registrarlo. Un'interruzione fra il 4 e il
5 non produce quindi una seconda classificazione, e un elemento classificato ma
non spostato viene semplicemente spostato.

Il riepilogo si **prenota prima di partire**: l'effetto si registra prima di
produrlo, quindi al massimo una volta al giorno. Un invio interrotto non si
ripete da solo (`pecdesk riepilogo --forza`).

**Degradare, non fingere.** Un errore transitorio lascia il messaggio in coda e
lo annota; una richiesta non valida lo sospende senza ritentare; un guasto
sistemico (chiave rifiutata, credito esaurito, cinque fallimenti di fila) ferma
le chiamate — ma **non** il riepilogo, che parte lo stesso elencando cosa è
arrivato senza giudizio e perché. Una mattina senza email è peggio di una
mattina con "dodici messaggi non classificati".

## Scelte prese dove le indicazioni lasciavano margine

* **Confidenza a livelli, non a numeri.** La confidenza numerica di un modello
  non è calibrata: un `0.72` non significa che sbaglia nel 28% dei casi, e
  invita a tarare soglie su un numero che non misura niente. `alta/media/bassa`
  più l'elenco esplicito dei motivi di incertezza dice di più ed è verificabile.
* **Il riepilogo parte anche quando non si è classificato niente.** Era scritto
  "il programma si ferma"; fermarsi in silenzio lascia il titolare senza
  informazione proprio la mattina in cui qualcosa non ha funzionato.
* **Gli estratti spostano il rischio, non lo eliminano.** Un termine a pagina
  quattro di un atto scansionato non finirà nell'estratto. La contromisura non è
  leggere di più, è non fidarsi del silenzio: `tipi_sempre_al_titolare` manda su
  certi tipi a prescindere, e ogni esito dichiara su quanto testo è stato dato
  il giudizio.
* **Le ricevute positive non arrivano in coda** (pecfetch le tiene nel proprio
  stato): pecdesk non può dire "l'invio del cliente è stato consegnato".
* **Seconda fase progettata, non implementata.** Le bozze di inoltro via IMAP
  richiederanno un `Message-ID` generato da pecdesk e scritto nella bozza
  *prima* dell'`APPEND`, registrato nello stato e ricercabile per
  `HEADER Message-ID` alla ripresa: `APPEND` può riuscire senza restituire un
  UID. In nessuna fase si scrive su una casella PEC.
