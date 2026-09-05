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

La cartella condivisa (`output_root`) va montata e resa scrivibile a
`pecfetch`. Con un mount CIFS in `/etc/fstab`:

```
//nas/pec /srv/pec/condivisa cifs credentials=/etc/pecfetch/smb.cred,uid=pecfetch,gid=pecfetch,file_mode=0664,dir_mode=0775,nounix,noserverino 0 0
```

`noserverino` evita inode duplicati; il rename di una cartella resta l'unico
meccanismo su cui pecfetch conta per l'atomicità, e su CIFS funziona purché
origine e destinazione stiano sullo stesso mount (lo staging `.tmp-pecfetch`
sta apposta dentro `output_root`).

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
