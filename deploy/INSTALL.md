# Installazione su Debian

Testato su Debian 12. Tutti i comandi come root.

## 1. Pacchetti di sistema

```sh
apt update
apt install -y python3 python3-venv python3-pip git \
               poppler-utils \
               tesseract-ocr tesseract-ocr-ita \
               openssl
```

* `poppler-utils` -> `pdftotext`, `pdftoppm`
* `tesseract-ocr` + `tesseract-ocr-ita` -> OCR degli atti scansionati
* `openssl` -> sbustamento delle firme `.p7m` (CAdES)

`poppler-utils` e `tesseract-ocr-ita` non sono facoltativi in pratica: senza,
il testo dei PDF (soprattutto quelli scansionati degli enti) non si estrae e
`pecfetch check` lo segnala.

## 2. Utente e cartelle

```sh
adduser --system --group --home /var/lib/pecfetch pecfetch
mkdir -p /etc/pecfetch /var/log/pecfetch /opt/pecfetch
chown pecfetch:pecfetch /var/lib/pecfetch /var/log/pecfetch
```

La cartella di output va creata e resa scrivibile a `pecfetch`:

```sh
mkdir -p /srv/pec/dati
chown pecfetch:pecfetch /srv/pec/dati
```

I dati restano in locale e lì vengono elaborati dal componente a valle.
L'atomicità della coda si regge su un `rename`, quindi lo staging
`.tmp-pecfetch` deve stare sullo stesso filesystem di `output_root`: ci sta
già dentro, quindi non c'è niente da fare finché `output_root` è una sola
directory.

## 3. Applicazione

```sh
git clone <url-del-repository> /opt/pecfetch/app
python3 -m venv /opt/pecfetch/venv
/opt/pecfetch/venv/bin/pip install --upgrade pip
/opt/pecfetch/venv/bin/pip install '/opt/pecfetch/app[extract]'
ln -sf /opt/pecfetch/venv/bin/pecfetch /usr/local/bin/pecfetch
```

## 4. Configurazione

```sh
cd /opt/pecfetch/app
cp config/pecfetch.example.toml  /etc/pecfetch/pecfetch.toml
cp config/caselle.example.toml   /etc/pecfetch/caselle.toml
cp config/secrets.example.toml   /etc/pecfetch/secrets.toml
chown root:pecfetch /etc/pecfetch/*.toml
chmod 640 /etc/pecfetch/secrets.toml /etc/pecfetch/caselle.toml
```

Poi:

```sh
sudo -u pecfetch /opt/pecfetch/venv/bin/pecfetch -c /etc/pecfetch/pecfetch.toml check --login
```

## 5. Prima esecuzione

Le caselle contengono anni di archivio: **prima si fissa la posizione**.

```sh
sudo -u pecfetch pecfetch -c /etc/pecfetch/pecfetch.toml init        # fissa la posizione, non scarica nulla
sudo -u pecfetch pecfetch -c /etc/pecfetch/pecfetch.toml run --dry-run
sudo -u pecfetch pecfetch -c /etc/pecfetch/pecfetch.toml run
```

## 6. Automazione

```sh
cd /opt/pecfetch/app
cp deploy/pecfetch.service deploy/pecfetch.timer /etc/systemd/system/
cp deploy/logrotate.pecfetch /etc/logrotate.d/pecfetch
systemctl daemon-reload
systemctl enable --now pecfetch.timer
systemctl list-timers pecfetch.timer
```

Il servizio è `Type=oneshot` e il lock su file impedisce sovrapposizioni anche
se un'esecuzione sfora l'intervallo del timer.

---

# pecdesk

Il consumatore della coda. Gira sulla stessa VM ma con un utente suo, perché ha
bisogni diversi: legge la coda, scrive solo in `esiti/` e `lavorati/`, e parla
con due servizi esterni (l'API di Anthropic e l'SMTP dello studio) con cui
pecfetch non parla mai.

## 1. Utente e cartelle

```sh
adduser --system --group --home /var/lib/pecdesk pecdesk
# deve poter leggere la coda di pecfetch e scrivere nei suoi due spazi
adduser pecdesk pecfetch
mkdir -p /etc/pecdesk/direttive /var/log/pecdesk /var/lib/pecdesk/riepiloghi
chown pecdesk:pecdesk /var/lib/pecdesk /var/log/pecdesk /var/lib/pecdesk/riepiloghi
mkdir -p /srv/pec/dati/esiti /srv/pec/dati/lavorati
chown pecdesk:pecfetch /srv/pec/dati/esiti /srv/pec/dati/lavorati
chmod 2775 /srv/pec/dati/esiti /srv/pec/dati/lavorati
```

`coda/` resta di proprietà di `pecfetch`, ma il gruppo deve poterci scrivere:
pecdesk non modifica i file, però sposta le cartelle lavorate fuori dalla coda,
e per farlo serve il permesso di scrittura sulla directory che le contiene.

```sh
chmod 2775 /srv/pec/dati/coda
```

L'archivio storico invece si legge e basta: l'unit systemd lo monta in sola
lettura, e il codice lo apre comunque con `mode=ro`.

## 2. Installazione

pecdesk vive nello stesso pacchetto, con un extra suo:

```sh
/opt/pecfetch/venv/bin/pip install '/opt/pecfetch/app[extract,pecdesk]'
ln -sf /opt/pecfetch/venv/bin/pecdesk /usr/local/bin/pecdesk
```

## 3. Configurazione e direttive

```sh
cd /opt/pecfetch/app
cp config/pecdesk.example.toml /etc/pecdesk/pecdesk.toml
cp direttive/regole.toml direttive/istruzioni.md /etc/pecdesk/direttive/
chown -R root:pecdesk /etc/pecdesk
chmod 640 /etc/pecdesk/pecdesk.toml
```

Le direttive vanno adattate allo studio — indirizzi veri in `[destinatari]`,
domini veri nelle regole — e **tenute sotto controllo di versione**: sono la
parte che cambierà più spesso, ed è quella che vorrai poter rileggere fra sei
mesi quando `pecdesk registro` dirà che qualcosa non torna.

## 4. Segreti

La chiave API e la password SMTP stanno fuori dal repository e non compaiono nei
log (un filtro cancella comunque qualunque `sk-ant-…` che ci finisse per errore).

```sh
cat > /etc/pecdesk/pecdesk.env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
PECDESK_SMTP_PASSWORD=...
EOF
chown root:pecdesk /etc/pecdesk/pecdesk.env
chmod 640 /etc/pecdesk/pecdesk.env
```

## 5. Verifica, poi prima esecuzione

```sh
sudo -u pecdesk pecdesk -c /etc/pecdesk/pecdesk.toml check --api --smtp
sudo -u pecdesk pecdesk -c /etc/pecdesk/pecdesk.toml run --prova
sudo -u pecdesk pecdesk -c /etc/pecdesk/pecdesk.toml run --limite 5
```

`--prova` stampa esattamente il materiale che verrebbe inviato al modello, i
tagli e la stima dei token, senza chiamare l'API, senza scrivere esiti e senza
spostare niente dalla coda. Vale la pena guardarlo prima di spendere il primo
euro, e ogni volta che si toccano le direttive.

Poi si legge quello che è uscito:

```sh
tail -n 3 /srv/pec/dati/esiti/$(date +%F).jsonl | python3 -m json.tool
cat /var/lib/pecdesk/riepiloghi/$(date +%F).txt
```

## 6. Automazione

```sh
cd /opt/pecfetch/app
cp deploy/pecdesk.service deploy/pecdesk.timer /etc/systemd/system/
cp deploy/logrotate.pecdesk /etc/logrotate.d/pecdesk
systemctl daemon-reload
systemctl enable --now pecdesk.timer
systemctl list-timers pecdesk.timer
```

Il timer scatta una volta al mattino: il riepilogo deve essere sul telefono del
titolare prima che apra lo studio. pecfetch continua a girare ogni quindici
minuti per conto suo — i due programmi non si aspettano a vicenda e non
condividono né stato né lock.

## 7. Manutenzione

* `pecdesk stato` elenca i messaggi **sospesi**: sono rimasti in coda dopo tre
  tentativi falliti e non verranno ritentati finché non li sblocchi con
  `pecdesk riprova <id>`.
* `pecdesk registro --giorni 30` confronta gli esiti con le correzioni e mostra
  dove il sistema sbaglia in modo sistematico: per regola, per cliente, per
  mittente, per tipo di documento, per livello di confidenza. Una quota di
  correzioni alta su una confidenza `alta` è il segnale che le direttive vanno
  riviste — non che vada alzata una soglia.
* Le correzioni si registrano con `pecdesk correggi <id> --campo ... --valore
  ...`: finiscono in `esiti/correzioni/`, **accanto** alla proposta originale e
  mai al suo posto.
* `lavorati/` cresce all'infinito per scelta: è l'unico posto dove ritrovare un
  messaggio dopo che è stato smistato. Ripulirlo è una decisione dello studio,
  non del programma.
