"""Scrittura del contratto di output.

Due livelli, come richiesto dal consumatore che lavora in due passate:

  ``indice/AAAA-MM-GG.jsonl``   un record per messaggio, compatto, per il triage;
  ``coda/<messaggio>/``          metadati completi, corpo, allegati, testo degli
                                 allegati, busta originale.

Regole di scrittura:
  * la comparsa di un messaggio è atomica: si monta tutto in ``.tmp-pecfetch``
    e si sposta con un rename sulla stessa unità;
  * il corpo sta in un file di testo a sé (``corpo.txt``), mai annegato in JSON;
  * l'indice si accresce in append, una riga per messaggio, senza lock;
  * ``esiti/`` è del consumatore: pecfetch la crea e non la tocca mai più;
  * eseguibili e script non vengono mai scritti: restano censiti nei metadati e
    integri dentro ``busta.eml``.

I nomi dei file vengono sanificati: non per compatibilità con altri sistemi, ma
perché un nome che arriva dal mittente non deve mai poter uscire dalla cartella
del messaggio.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import __version__, permessi as perm, tempo
from .extract import ExtractorSettings, extract_text
from .naming import safe_filename, slugify, unique_filename
from .safety import classify_file
from .pec import ParsedMessage, certified_or_best_date, receipt_class

#: versione 2: date rese tutte nel fuso dichiarato e indirizzi normalizzati.
#: La forma dei record non cambia, il significato dei campi sì, e chi legge
#: deve poterlo distinguere senza confrontare i valori.
SCHEMA_INDEX = "pecfetch/indice/2"
SCHEMA_MESSAGE = "pecfetch/messaggio/2"

DIR_QUEUE = "coda"
DIR_INDEX = "indice"
DIR_OUTCOMES = "esiti"
DIR_STAGING = ".tmp-pecfetch"

FILE_METADATA = "messaggio.json"
FILE_BODY = "corpo.txt"
FILE_ENVELOPE = "busta.eml"
FILE_POSTACERT = "postacert.eml"
FILE_DATICERT = "daticert.xml"
DIR_ATTACHMENTS = "allegati"


@dataclass
class WriteResult:
    msg_id: str
    content_dir: str          # relativo alla radice
    index_file: str           # relativo alla radice
    index_record: dict
    already_present: bool = False
    body_text: str = ""            # per l'archivio full-text, non riscritto su disco
    attachment_text: str = ""


class OutputWriter:
    """Unico punto di scrittura sulla cartella di output."""

    #: le cartelle che il consumatore deve poter scrivere: ci sposta dentro o
    #: fuori intere cartelle, e per farlo serve il permesso sulla directory
    CONDIVISE = (DIR_QUEUE, DIR_OUTCOMES)

    def __init__(self, root: Path, settings: ExtractorSettings,
                 body_max_chars: int = 200_000,
                 attachment_store_max_bytes: int = 50 * 1024 * 1024,
                 timezone: str = tempo.DEFAULT_TZ,
                 permissions: perm.Permessi | None = None,
                 layout: bool = True):
        self.root = Path(root)
        self.settings = settings
        self.body_max_chars = body_max_chars
        self.attachment_store_max_bytes = attachment_store_max_bytes
        # un fuso sbagliato è un errore di configurazione, non un ripiego su UTC
        self.tz = tempo.zona(timezone)
        self.permessi = permissions or perm.Permessi()
        # in prova a vuoto non si crea nemmeno l'albero: "non scrive nulla"
        # deve valere per l'albero prodotto, non solo per il riepilogo
        if layout:
            self.ensure_layout()

    # -- struttura -------------------------------------------------------
    def ensure_layout(self) -> None:
        for name in (DIR_QUEUE, DIR_INDEX, DIR_OUTCOMES, DIR_STAGING):
            modo = (self.permessi.shared_dir_mode if name in self.CONDIVISE
                    else self.permessi.dir_mode)
            perm.crea_dir(self.root / name, modo, self.permessi)
        contract = self.root / "CONTRATTO.md"
        testo = self._contratto()
        # il contratto dichiara fuso e permessi effettivi: se cambiano in
        # configurazione, il file in radice deve dirlo, non restare al vecchio
        if not contract.exists() or contract.read_text(encoding="utf-8") != testo:
            contract.write_text(testo, encoding="utf-8")
        perm.applica_file(contract, self.permessi)
        readme = self.root / DIR_OUTCOMES / "LEGGIMI.md"
        if not readme.exists():
            readme.write_text(_OUTCOMES_README, encoding="utf-8")
        perm.applica_file(readme, self.permessi)

    def applica_permessi(self) -> list[str]:
        """Riporta l'albero già prodotto al modello dichiarato.

        Serve a chi aggiorna: le cartelle scritte prima di questa versione hanno
        i permessi che capitavano, e non si sistemano a mano una per una.
        """
        self.ensure_layout()
        for name in (DIR_QUEUE, DIR_INDEX):
            radice = self.root / name
            if radice.is_dir():
                modo = (self.permessi.shared_dir_mode if name in self.CONDIVISE
                        else self.permessi.dir_mode)
                perm.applica_albero(radice, self.permessi, root_mode=modo)
        # di esiti/ si sistema la cartella, non quello che c'è dentro: quei file
        # li ha scritti il consumatore e non sono nostri da toccare
        perm.crea_dir(self.root / DIR_OUTCOMES, self.permessi.shared_dir_mode,
                      self.permessi)
        perm.applica_file(self.root / "CONTRATTO.md", self.permessi)
        return self.divergenze()

    def divergenze(self) -> list[str]:
        """I percorsi che non corrispondono al modello. Non corregge niente."""
        fuori: list[str] = []
        for name in (DIR_QUEUE, DIR_INDEX):
            radice = self.root / name
            if not radice.is_dir():
                continue
            atteso = (self.permessi.shared_dir_mode if name in self.CONDIVISE
                      else self.permessi.dir_mode)
            attuale = radice.stat().st_mode & 0o7777
            if attuale != atteso:
                fuori.append(f"{radice}: {attuale:04o} invece di {atteso:04o}")
            fuori += perm.divergenze(radice, self.permessi)
        esiti = self.root / DIR_OUTCOMES
        if esiti.is_dir():
            attuale = esiti.stat().st_mode & 0o7777
            if attuale != self.permessi.shared_dir_mode:
                fuori.append(f"{esiti}: {attuale:04o} invece di "
                             f"{self.permessi.shared_dir_mode:04o}")
        return fuori[:20]

    def _contratto(self) -> str:
        esempio = tempo.reso(datetime(2026, 9, 5, 10, 31, 0, tzinfo=self.tz), self.tz)
        p = self.permessi
        return _CONTRACT_TEXT.format(
            fuso=self.tz.key,
            esempio=esempio,
            permessi=(
                f"    cartelle              {p.dir_mode & 0o7777:04o}\n"
                f"    file                  {p.file_mode:04o}\n"
                f"    coda/ ed esiti/       {p.shared_dir_mode:04o}"
                + (f"\n    gruppo                {p.group}" if p.group else "")
            ),
        )

    def index_path(self, when: datetime | None = None) -> Path:
        when = when or datetime.now(self.tz)
        try:
            local = when.astimezone(self.tz)
        except Exception:
            local = when
        return self.root / DIR_INDEX / f"{local:%Y-%m-%d}.jsonl"

    # -- scrittura di un messaggio ---------------------------------------
    def write_message(self, pm: ParsedMessage, raw: bytes, account, msg_id: str,
                      uidvalidity: int, uid: int, fetched_at: str,
                      extraction: bool = True) -> WriteResult:
        """Monta la cartella del messaggio e la rende visibile con un rename."""
        dir_name = self._dir_name(pm, account, msg_id)
        final_dir = self.root / DIR_QUEUE / dir_name

        staging = Path(tempfile.mkdtemp(prefix=f"{msg_id}-", dir=self.root / DIR_STAGING))
        try:
            record, texts = self._materialize(
                staging, pm, raw, account, msg_id, uidvalidity, uid, fetched_at,
                dir_name, extraction,
            )
            perm.applica_albero(staging, self.permessi)
            _fsync_tree(staging)
            already = False
            try:
                os.rename(staging, final_dir)
            except OSError:
                if final_dir.exists():
                    # Stesso msg_id = stesso contenuto sulla stessa casella:
                    # il messaggio è già uscito, non lo si riscrive.
                    already = True
                    shutil.rmtree(staging, ignore_errors=True)
                else:
                    raise
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        return WriteResult(
            msg_id=msg_id,
            content_dir=f"{DIR_QUEUE}/{dir_name}",
            index_file="",
            index_record=record,
            already_present=already,
            body_text=texts.get("corpo", ""),
            attachment_text=texts.get("allegati", ""),
        )

    def append_index(self, record: dict, when: datetime | None = None) -> str:
        """Aggiunge una riga all'indice del giorno. Ritorna il path relativo."""
        path = self.index_path(when)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        fd = perm.apri_append(path, self.permessi)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return f"{DIR_INDEX}/{path.name}"

    def cleanup_staging(self, max_age_seconds: int = 86_400) -> int:
        """Rimuove i montaggi rimasti a metà da esecuzioni interrotte."""
        staging = self.root / DIR_STAGING
        removed = 0
        now = datetime.now().timestamp()
        for entry in staging.iterdir() if staging.is_dir() else []:
            try:
                if now - entry.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed

    # -- interno ---------------------------------------------------------
    def _dir_name(self, pm: ParsedMessage, account, msg_id: str) -> str:
        when = certified_or_best_date(pm)
        try:
            local = when.astimezone(self.tz)
        except Exception:
            local = when
        subject_slug = slugify(pm.subject or pm.envelope_subject or "senza-oggetto")[:44]
        parts = [
            f"{local:%Y%m%d-%H%M}",
            slugify(account.id)[:24],
            subject_slug or "senza-oggetto",
            msg_id[:12],
        ]
        return safe_filename("_".join(p for p in parts if p))

    def _materialize(self, staging: Path, pm: ParsedMessage, raw: bytes, account,
                     msg_id: str, uidvalidity: int, uid: int, fetched_at: str,
                     dir_name: str, extraction: bool = True) -> tuple[dict, dict]:
        """Ritorna (record d'indice, testi utili all'indicizzazione full-text)."""
        (staging / DIR_ATTACHMENTS).mkdir(parents=True, exist_ok=True)

        # 1. sorgente originale, sempre e comunque
        (staging / FILE_ENVELOPE).write_bytes(raw)
        if pm.postacert_raw:
            (staging / FILE_POSTACERT).write_bytes(pm.postacert_raw)
        if pm.daticert_raw:
            (staging / FILE_DATICERT).write_bytes(pm.daticert_raw)

        # 2. corpo in un file di testo a sé
        body = pm.body_text or ""
        body_truncated = False
        if len(body) > self.body_max_chars:
            body = body[: self.body_max_chars] + (
                f"\n\n[…troncato da pecfetch: il corpo completo è in {FILE_ENVELOPE}]"
            )
            body_truncated = True
        (staging / FILE_BODY).write_text(body, encoding="utf-8")

        # 3. allegati + testo estratto accanto a ciascuno
        taken: set[str] = set()
        attachments_meta: list[dict] = []
        extracted_texts: list[str] = []
        settings = self.settings
        if not extraction:
            settings = replace(self.settings, enabled=False)

        for att in pm.attachments:
            stored_name = unique_filename(att.filename or "allegato", taken)
            entry: dict = {
                "nome": att.filename,
                "nome_file": stored_name,
                "percorso": f"{DIR_ATTACHMENTS}/{stored_name}",
                "content_type": att.content_type,
                "byte": att.size,
                "sha256": att.sha256,
            }
            if att.inline:
                entry["inline"] = True

            verdict = classify_file(att.filename, att.content_type, att.payload,
                                    settings.active_extra)
            if verdict.macro_enabled:
                entry["contenuto_attivo"] = True
            if verdict.suspicious:
                entry["contenuto_sospetto"] = True
                entry["nota"] = verdict.reason

            # Un tipo attivo non viene mai scritto su disco: resta censito qui e
            # integro dentro busta.eml, dove non può essere raggiunto per sbaglio.
            if verdict.active and settings.block_active_types:
                entry["contenuto_attivo"] = True
                self._not_stored(entry, f"tipo attivo: {verdict.reason}",
                                 "blocked_type")
                attachments_meta.append(entry)
                continue

            if att.size > self.attachment_store_max_bytes:
                self._not_stored(entry, "oltre la soglia di salvataggio",
                                 "skipped_too_large")
                attachments_meta.append(entry)
                continue

            (staging / DIR_ATTACHMENTS / stored_name).write_bytes(att.payload)
            entry["salvato"] = True
            result = extract_text(att.payload, att.filename, att.content_type,
                                  settings)
            entry["testo"] = result.as_dict()
            if result.text:
                extracted_texts.append(result.text)
                text_name = f"{stored_name}.txt"
                (staging / DIR_ATTACHMENTS / text_name).write_text(
                    result.text, encoding="utf-8"
                )
                entry["testo"]["percorso"] = f"{DIR_ATTACHMENTS}/{text_name}"

            if result.members:
                entry["archivio"] = self._write_members(
                    staging, stored_name, result, extracted_texts
                )
            attachments_meta.append(entry)

        record = self._index_record(
            pm, account, msg_id, uidvalidity, uid, fetched_at, dir_name,
            attachments_meta, len(body), body_truncated,
        )
        metadata = self._full_metadata(pm, record, raw, attachments_meta)
        (staging / FILE_METADATA).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return record, {"corpo": body, "allegati": "\n".join(extracted_texts)}

    @staticmethod
    def _not_stored(entry: dict, motivo: str, stato: str) -> None:
        """L'allegato non finisce su disco, ma resta censito per intero."""
        entry["salvato"] = False
        entry["motivo"] = motivo
        entry.pop("percorso", None)
        entry.pop("nome_file", None)
        entry["testo"] = {"method": "none", "status": stato, "chars": 0}

    def _write_members(self, staging: Path, stored_name: str, result,
                       extracted_texts: list[str]) -> dict:
        """Scrive le voci idonee di un archivio.

        Due regole, entrambe non negoziabili: il nome sul disco lo generiamo
        noi, numerato progressivamente, e il percorso dichiarato dentro
        l'archivio finisce solo nei metadati come dato — mai come percorso.
        """
        member_dir = safe_filename(f"{stored_name}.d")
        target = staging / DIR_ATTACHMENTS / member_dir
        target.mkdir(parents=True, exist_ok=True)

        member_taken: set[str] = set()
        membri: list[dict] = []
        scritti = 0
        for position, (declared, member) in enumerate(_leaf_members(result.members), 1):
            meta = member.as_dict()
            meta["percorso_dichiarato"] = declared
            if member.stored and member.data:
                name = unique_filename(f"{position:03d}_{member.name}", member_taken)
                (target / name).write_bytes(member.data)
                scritti += 1
                meta["nome_file"] = name
                meta["percorso"] = f"{DIR_ATTACHMENTS}/{member_dir}/{name}"
                text = member.result.text if member.result is not None else ""
                if text:
                    extracted_texts.append(text)
                    (target / f"{name}.txt").write_text(text, encoding="utf-8")
                    meta.setdefault("testo", {})["percorso"] = (
                        f"{DIR_ATTACHMENTS}/{member_dir}/{name}.txt"
                    )
            else:
                meta["salvato"] = False
            membri.append(meta)

        if not scritti:
            # niente da materializzare: non si lascia in giro una cartella vuota
            try:
                target.rmdir()
            except OSError:
                pass

        report = result.archive.as_dict() if result.archive is not None else {}
        return {**report, "cartella": f"{DIR_ATTACHMENTS}/{member_dir}",
                "membri": membri}

    def _index_record(self, pm: ParsedMessage, account, msg_id: str,
                      uidvalidity: int, uid: int, fetched_at: str, dir_name: str,
                      attachments_meta: list[dict], body_chars: int,
                      body_truncated: bool) -> dict:
        content_dir = f"{DIR_QUEUE}/{dir_name}"
        record = {
            "schema": SCHEMA_INDEX,
            "id": msg_id,
            "acquisito_il": fetched_at,
            "casella": {
                "id": account.id,
                "etichetta": account.display,
                "indirizzo": account.address,
                "cliente": account.client_id or account.id,
            },
            "tipo": pm.msg_type,
            "certificato": pm.certified,
            # tutte e tre nello stesso fuso: chi legge confronta orari
            # confrontabili, senza conversioni a mente
            "data": {
                "certificata": tempo.reso(pm.date_certified, self.tz),
                "invio": tempo.reso(pm.date_sent, self.tz),
                "ricezione": tempo.reso(pm.date_received, self.tz),
            },
            "mittente": {
                "indirizzo": pm.from_addr.get("address", ""),
                "nome": pm.from_addr.get("name", ""),
                "dominio": pm.from_addr.get("domain", ""),
            },
            "destinatari": [a.get("address", "") for a in pm.to if a.get("address")],
            "copia": [a.get("address", "") for a in pm.cc if a.get("address")],
            "oggetto": pm.subject,
            "allegati": [
                {
                    "nome": a["nome"],
                    "content_type": a.get("content_type", ""),
                    "byte": a.get("byte", 0),
                    "testo": a.get("testo", {}).get("status", ""),
                    "metodo": a.get("testo", {}).get("method", ""),
                    "caratteri": a.get("testo", {}).get("chars", 0),
                    **({"salvato": False} if a.get("salvato") is False else {}),
                    **({"voci": len(a["archivio"].get("membri", []))}
                       if "archivio" in a else {}),
                }
                for a in attachments_meta
            ],
            "message_id": pm.message_id,
            "identificativo_pec": pm.pec_identifier,
            "gestore": pm.gestore,
            "contenuto": {
                "cartella": content_dir,
                "metadati": f"{content_dir}/{FILE_METADATA}",
                "corpo": f"{content_dir}/{FILE_BODY}",
                "busta": f"{content_dir}/{FILE_ENVELOPE}",
                "caratteri_corpo": body_chars,
                "corpo_troncato": body_truncated,
                "corpo_origine": pm.body_source,
            },
            "imap": {"uidvalidity": uidvalidity, "uid": uid},
        }
        if pm.receipt_kind:
            record["ricevuta"] = {
                "tipo": pm.receipt_kind,
                "classe": receipt_class(pm.msg_type) or "",
                "riferimento_message_id": pm.ref_message_id,
                "errore": pm.error_detail,
            }
        if pm.flags:
            record["note"] = list(pm.flags)
        return record

    def _full_metadata(self, pm: ParsedMessage, record: dict, raw: bytes,
                       attachments_meta: list[dict]) -> dict:
        dc = pm.daticert
        meta = {
            "schema": SCHEMA_MESSAGE,
            "generato_da": f"pecfetch/{__version__}",
            **{k: v for k, v in record.items() if k != "schema"},
            "destinatari_completi": pm.to,
            "copia_completa": pm.cc,
            "rispondi_a": pm.reply_to,
            "busta": {
                "oggetto": pm.envelope_subject,
                "mittente": pm.envelope_from,
                "message_id": pm.envelope_message_id,
                "byte": len(raw),
                "file": FILE_ENVELOPE,
                "postacert": FILE_POSTACERT if pm.postacert_raw else None,
                "daticert": FILE_DATICERT if pm.daticert_raw else None,
            },
            "allegati": attachments_meta,
            "header": pm.headers,
        }
        if dc:
            meta["daticert"] = {
                "tipo": dc.tipo,
                "errore": dc.errore,
                "mittente": dc.mittente,
                "destinatari": dc.destinatari,
                "oggetto": dc.oggetto,
                "gestore": dc.gestore,
                "data": tempo.reso(dc.data, self.tz),
                "identificativo": dc.identificativo,
                "msgid": dc.msgid,
                "ricevuta_tipo": dc.ricevuta_tipo,
                "consegna": dc.consegna,
                "errore_esteso": dc.errore_esteso,
            }
        return meta


def _leaf_members(members, prefix: str = ""):
    """Appiattisce l'albero delle voci: si materializzano solo le foglie.

    Un archivio annidato non viene riscritto come file: si scrivono le sue voci.
    """
    for member in members:
        declared = f"{prefix}{member.declared_path or member.name}"
        nested = member.result.members if member.result is not None else []
        if nested:
            yield from _leaf_members(nested, f"{declared}/")
        else:
            yield declared, member


def _fsync_tree(path: Path) -> None:
    """Forza su disco prima del rename: niente file a metà nella coda."""
    for entry in sorted(path.rglob("*")):
        if entry.is_file():
            try:
                fd = os.open(entry, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                continue
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


_CONTRACT_TEXT = """# Contratto di output di pecfetch

Cartella prodotta da `pecfetch`. Sola lettura per chiunque non sia pecfetch,
tranne `esiti/` che è del consumatore.

## Date e fuso orario

**Tutte** le date esposte — nell'indice, nei metadati, ovunque — sono ISO 8601
con l'offset di **{fuso}**, con precisione al secondo:

    {esempio}

Non c'è nessun campo in UTC e nessun campo senza offset: due date di questo
albero si confrontano fra loro senza conversioni. Vale per `data.certificata`,
`data.invio`, `data.ricezione`, `acquisito_il` e `daticert.data`. I nomi dei
file d'indice e delle cartelle usano lo stesso fuso, quindi il giorno del nome
è il giorno della data.

L'offset dichiarato dal gestore resta leggibile dentro `daticert.xml` e
`busta.eml`, che non vengono mai riscritti: qui cambia come la data è resa, non
quale istante indica.

## Indirizzi

Gli indirizzi esposti (`mittente.indirizzo`, `destinatari`, `copia`,
`casella.indirizzo`, `daticert.mittente`, `daticert.destinatari`) sono sempre in
minuscolo, parte locale compresa: nessun gestore PEC italiano tratta le caselle
come sensibili alle maiuscole, e chi confronta questi campi non deve
preoccuparsene. La forma scritta dal mittente non si perde: resta negli header
conservati in `messaggio.json` (`header.From` e `header.To` per la busta,
`header.postacert.From` e `header.postacert.To` per il messaggio interno) e
integra dentro `busta.eml`.

## Permessi

{permessi}

`coda/` ed `esiti/` sono scrivibili dal gruppo perché il consumatore, che gira
con un'altra identità, deve poter spostare fuori dalla coda le cartelle che ha
lavorato e scrivere i propri esiti. Il bit setgid tiene nel gruppo giusto ciò
che il consumatore crea. Chi espone questa radice in sola lettura sulla rete lo
fa con un servizio che appartiene al gruppo.

## Struttura

    indice/AAAA-MM-GG.jsonl   un record JSON per riga, un messaggio per record.
                              Serve al triage: si legge in blocco senza aprire
                              altro. I record sono indipendenti e in sola
                              append.
    coda/<messaggio>/         il contenuto completo di un messaggio:
        messaggio.json        metadati completi (superset del record d'indice)
        corpo.txt             corpo del messaggio, testo normalizzato
        allegati/<file>       allegato con il nome originale sanificato
        allegati/<file>.txt   testo estratto dall'allegato (quando ricavabile)
        allegati/<file>.d/    voci estratte da un allegato compresso,
                              rinominate NNN_<nome> da pecfetch
        busta.eml             sorgente originale integrale
        postacert.eml         messaggio interno alla busta di trasporto
        daticert.xml          metadati certificati del gestore
    esiti/                    riservata al consumatore, append-only
    .tmp-pecfetch/            area di montaggio di pecfetch, da ignorare

## Garanzie

* Una cartella dentro `coda/` compare per intero o non compare: viene montata
  altrove e spostata con un rename.
* `id` è la chiave stabile del messaggio: identico fra indice, metadati ed
  esiti. Deriva dal contenuto della busta e dalla casella ricevente.
* Le ricevute positive (accettazione, presa in carico, avvenuta consegna) NON
  compaiono qui: sono registrate nello stato locale di pecfetch. In output
  arrivano solo le ricevute negative, cioè gli invii falliti.
* Il consumatore può spostare o cancellare quello che ha processato: pecfetch
  non riscrive nulla di già uscito, perché la verità su cosa è stato scaricato
  sta nel suo stato locale, non nella presenza dei file.
* `corpo.txt` è sempre testo: se il messaggio aveva solo HTML, è stato
  convertito. Se il corpo è stato troncato, il record d'indice lo dichiara in
  `contenuto.corpo_troncato`.
* Il testo degli allegati riporta metodo ed esito dell'estrazione
  (`allegati[].metodo`, `allegati[].testo`): `pdf_ocr` significa OCR, quindi
  testo potenzialmente incerto.
* Se lo spazio libero scende sotto la soglia configurata, pecfetch smette di
  scrivere **prima** di cominciare un messaggio e lo annota nel proprio stato:
  in questo albero non compaiono mai messaggi troncati per disco pieno.

## Allegati che non sono stati scritti

Un allegato con `"salvato": false` esiste, ma il suo file non è stato creato.
Il campo `motivo` dice perché, e il contenuto integrale resta dentro
`busta.eml`. Succede in tre casi:

* **tipo attivo** — eseguibili, script, collegamenti, immagini disco. Non
  vengono mai materializzati, né come allegato diretto né dall'interno di un
  archivio: `"contenuto_attivo": true`. Se il nome mentiva sul contenuto (un
  `.pdf` che comincia per `MZ`) c'è anche `"contenuto_sospetto": true`.
* **oltre la soglia di salvataggio** — allegato troppo grande.
* **tipo da cui non si ricava testo**, per le sole voci interne agli archivi.

I documenti office con macro vengono invece salvati e letti (si smontano come
ZIP + XML, nessuna macro viene eseguita) ma sono marcati `contenuto_attivo`.

## Archivi

Un allegato compresso porta un campo `archivio` con il censimento completo:
formato, numero di voci, byte espansi, rapporto di espansione, e per ogni voce
il `percorso_dichiarato` così com'era scritto **dentro** l'archivio. Quel
percorso è un dato, non un percorso: i file veri stanno in `allegati/<file>.d/`
con nomi generati da pecfetch.

Se un limite scatta (rapporto di espansione, byte totali, numero di voci,
annidamento), la lettura si ferma, `limite_superato` dice quale, e quello che si
era già letto resta valido. `rar`, `7z` e le immagini disco non vengono aperti
per scelta: `unsupported_archive`.

In nessuno di questi casi il messaggio manca: esce comunque, annotato.
"""


_OUTCOMES_README = """# esiti/

Spazio riservato al componente a valle. pecfetch crea questa cartella e non ci
scrive, non la legge e non ne dipende in alcun modo.

Convenzione suggerita: un file JSONL al giorno, `AAAA-MM-GG.jsonl`, append-only,
un record per messaggio processato, con almeno:

    {"id": "<id del messaggio, come nell'indice>",
     "elaborato_il": "2026-09-05T10:31:00+02:00",
     "esito": "...", "note": "..."}

I file di input non vanno modificati: restano immutabili.
"""

__all__ = ["OutputWriter", "WriteResult", "SCHEMA_INDEX", "SCHEMA_MESSAGE",
           "DIR_QUEUE", "DIR_INDEX", "DIR_OUTCOMES", "DIR_STAGING"]
