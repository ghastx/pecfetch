"""Orchestrazione di un'esecuzione.

Ordine delle operazioni su ogni messaggio, che è il cuore della garanzia
"nessuna perdita":

    1. scarico con BODY.PEEK      -> nessun flag toccato sul server
    2. impronta del contenuto     -> deduplica indipendente dagli UID
    3. registro il messaggio      -> lo stato sa che sto lavorando su di lui
    4. scrivo i file              -> montaggio + rename atomico
    5. riga d'indice, archivio    -> una fase per volta, confermata a parte
    6. avanzo il cursore          -> SOLO ORA

Se qualcosa si rompe fra il 3 e il 6, alla prossima esecuzione il messaggio
viene riscaricato: nel dubbio si riscarica, non si salta. Le riscritture sono
idempotenti perché il nome della cartella deriva dal contenuto.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import spazio, tempo
from .archive import Archive
from .config import Account, Config
from .imapclient import ImapError, ImapReader
from .output import OutputWriter
from .pec import (
    RECEIPTS_POSITIVE,
    certified_or_best_date,
    is_output_type,
    parse_pec,
)
from .state import (
    STATUS_DONE,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    State,
    content_hash,
    message_key,
)

log = logging.getLogger("pecfetch.pipeline")

MODE_RUN = "run"
MODE_INIT = "init"
MODE_BACKFILL = "backfill"

#: dopo tanti tentativi falliti sullo stesso messaggio si scrive comunque,
#: rinunciando all'estrazione del testo, per non bloccare la casella.
MAX_ATTEMPTS_BEFORE_DEGRADED = 3

#: marca ogni riga prodotta da una prova a vuoto, non solo il riepilogo finale
PROVA = "PROVA - "


@dataclass
class AccountResult:
    account_id: str
    ok: bool = True
    #: messaggi davvero scritti in coda. In prova a vuoto resta a zero.
    written: int = 0
    #: messaggi che si scaricherebbero: si riempie solo in prova a vuoto
    candidates: int = 0
    receipts: int = 0
    duplicates: int = 0
    skipped: int = 0
    examined: int = 0
    remaining: int = 0
    error: str = ""
    uidvalidity_reset: bool = False
    duration: float = 0.0


@dataclass
class RunSummary:
    mode: str
    started_at: str = ""
    results: list[AccountResult] = field(default_factory=list)

    @property
    def accounts_ok(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def accounts_err(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def written(self) -> int:
        return sum(r.written for r in self.results)

    @property
    def candidates(self) -> int:
        return sum(r.candidates for r in self.results)

    @property
    def receipts(self) -> int:
        return sum(r.receipts for r in self.results)

    @property
    def duplicates(self) -> int:
        return sum(r.duplicates for r in self.results)

    @property
    def remaining(self) -> int:
        return sum(r.remaining for r in self.results)

    @property
    def errors(self) -> list[tuple[str, str]]:
        return [(r.account_id, r.error) for r in self.results if not r.ok]


def default_connect(account: Account) -> ImapReader:
    return ImapReader(
        host=account.host,
        port=account.port,
        username=account.username,
        password=account.password,
        use_ssl=account.ssl,
        starttls=account.starttls,
        timeout=account.timeout,
    )


class Pipeline:
    def __init__(self, cfg: Config, state: State, writer: OutputWriter,
                 archive: Archive, connect=default_connect, run_id: int | None = None):
        self.cfg = cfg
        self.state = state
        self.writer = writer
        self.archive = archive
        self.connect = connect
        self.run_id = run_id
        self.tz = tempo.zona(cfg.timezone)

    # -- spazio ----------------------------------------------------------
    def _spazio(self, margine: int = 0) -> tuple[bool, str]:
        """C'è ancora posto per scrivere? Si guarda prima, non dopo.

        Si controllano entrambi i filesystem coinvolti: un disco pieno non
        blocca solo la coda, blocca anche lo stato, che è la parte che non può
        fallire perché è l'unica verità su cosa è già stato scaricato.
        """
        return spazio.sufficiente(
            (self.cfg.output_root, self.cfg.state_dir),
            self.cfg.min_free_bytes, margine,
        )

    def _oggi(self) -> date:
        """Il giorno nel fuso dichiarato: lo stesso che nomina indice e cartelle."""
        return tempo.oggi(self.tz)

    # -- esecuzione ------------------------------------------------------
    def run(self, mode: str = MODE_RUN, accounts: list[Account] | None = None,
            since: date | None = None, lookback_days: int | None = None,
            limit: int | None = None, dry_run: bool = False) -> RunSummary:
        summary = RunSummary(mode=mode, started_at=self.state.ora())
        accounts = accounts if accounts is not None else list(self.cfg.accounts)
        todo = [a for a in accounts if a.enabled]
        skipped_accounts = [a.id for a in accounts if not a.enabled]
        if skipped_accounts:
            log.info("caselle disabilitate, saltate: %s", ", ".join(skipped_accounts))

        ok_spazio, motivo = self._spazio()
        if not ok_spazio and dry_run:
            # una prova a vuoto non si ferma, ma deve dirlo
            log.warning("%sspazio insufficiente: un'esecuzione vera si fermerebbe "
                        "senza scaricare niente (%s)", PROVA, motivo)
        elif not ok_spazio:
            # Meglio non cominciare che cominciare e lasciare a metà: un disco
            # pieno fa fallire anche le scritture di stato.
            log.error("spazio insufficiente, nessun messaggio scaricato: %s", motivo)
            for account in todo:
                risultato = AccountResult(account_id=account.id, ok=False,
                                          error=f"spazio insufficiente: {motivo}")
                summary.results.append(risultato)
                if self.run_id is not None:
                    self.state.log_error(self.run_id, account.id, "spazio", motivo)
            return summary

        for account in todo:
            started = time.monotonic()
            result = AccountResult(account_id=account.id)
            try:
                self._process_account(account, result, mode, since,
                                      lookback_days, limit, dry_run)
            except ImapError as exc:
                # Una casella rotta non ferma le altre.
                result.ok = False
                result.error = str(exc)
                log.error("[%s] %s", account.id, exc)
            except Exception as exc:  # difetto nostro: stessa regola
                result.ok = False
                result.error = f"errore interno: {exc}"
                log.exception("[%s] errore interno", account.id)
            result.duration = time.monotonic() - started
            if not dry_run:
                self.state.mark_run(account.id, account.folder, result.ok,
                                    result.error or None)
                if not result.ok and self.run_id is not None:
                    self.state.log_error(self.run_id, account.id, mode, result.error)
            summary.results.append(result)
            # In prova a vuoto non si è scritto niente: il verbo cambia, perché
            # chi rilegge questo log fra sei mesi non deve concludere di avere
            # in archivio messaggi che non esistono.
            log.info(
                "%s[%s] %s: %d %s, %d ricevute positive, %d duplicati, "
                "%d esaminati, %d rimasti (%.1fs)",
                PROVA if dry_run else "",
                account.id, "ok" if result.ok else "ERRORE",
                result.candidates if dry_run else result.written,
                "da scaricare" if dry_run else "scritti",
                result.receipts, result.duplicates, result.examined,
                result.remaining, result.duration,
            )
        return summary

    # -- una casella -----------------------------------------------------
    def _process_account(self, account: Account, result: AccountResult, mode: str,
                         since: date | None, lookback_days: int | None,
                         limit: int | None, dry_run: bool) -> None:
        reader = self._connect_with_retry(account)
        try:
            uidvalidity = reader.examine(account.folder)
            cursor = self.state.get_cursor(account.id, account.folder)
            log.debug("[%s] uidvalidity=%s cursore=%s messaggi=%s",
                      account.id, uidvalidity, cursor.last_uid, reader.exists)

            if mode == MODE_INIT:
                self._initialize(account, reader, uidvalidity, result,
                                 lookback_days, dry_run)
                return

            uids = self._select_uids(account, reader, cursor, uidvalidity, mode,
                                     since, result, dry_run)
            budget = limit if limit is not None else self.cfg.max_messages_per_run
            if budget and len(uids) > budget:
                result.remaining = len(uids) - budget
                uids = uids[:budget]
            result.examined = len(uids)
            if not uids:
                return

            metas = reader.fetch_meta(uids)
            for posizione, uid in enumerate(uids):
                meta = metas.get(uid)

                # Guardia sullo spazio, prima di toccare lo stato e quindi senza
                # consumare i tentativi del messaggio: se non c'è posto si
                # rifiuta pulito, si annota, e il cursore resta dov'è. Alla
                # prossima esecuzione si riprende da qui.
                if not dry_run:
                    ok_spazio, motivo = self._spazio(meta.size if meta else 0)
                    if not ok_spazio:
                        rimasti = len(uids) - posizione
                        result.remaining += rimasti
                        result.examined -= rimasti
                        result.ok = False
                        result.error = f"spazio insufficiente: {motivo}"
                        log.error(
                            "[%s] mi fermo a UID %s: spazio insufficiente (%s). "
                            "%d messaggi restano sulla casella.",
                            account.id, uid, motivo, rimasti,
                        )
                        if self.run_id is not None:
                            self.state.log_error(self.run_id, account.id,
                                                 "spazio", motivo)
                        return

                if meta and meta.size > self.cfg.max_message_bytes:
                    log.error(
                        "%s[%s] UID %s ignorato: %d byte oltre il limite di %d",
                        PROVA if dry_run else "",
                        account.id, uid, meta.size, self.cfg.max_message_bytes,
                    )
                    if self.run_id is not None and not dry_run:
                        self.state.log_error(
                            self.run_id, account.id, "fetch",
                            f"UID {uid} troppo grande ({meta.size} byte)",
                        )
                    result.skipped += 1
                    if not dry_run:
                        self.state.advance_uid(account.id, account.folder,
                                               uidvalidity, uid)
                    continue

                if dry_run:
                    log.info("%s[%s] scaricherei UID %s (%s byte)",
                             PROVA, account.id, uid, meta.size if meta else "?")
                    result.candidates += 1
                    continue

                self._process_message(account, reader, uidvalidity, uid, meta, result)
        finally:
            reader.close()

    def _connect_with_retry(self, account: Account) -> ImapReader:
        last: Exception | None = None
        for attempt in range(max(1, self.cfg.connect_retries + 1)):
            reader = self.connect(account)
            try:
                reader.connect()
                return reader
            except ImapError as exc:
                last = exc
                reader.close()
                if attempt < self.cfg.connect_retries:
                    delay = 2 ** attempt
                    log.warning("[%s] tentativo %d fallito (%s), riprovo fra %ds",
                                account.id, attempt + 1, exc, delay)
                    time.sleep(delay)
        raise last if last else ImapError("connessione fallita")

    # -- selezione dei messaggi ------------------------------------------
    def _initialize(self, account: Account, reader: ImapReader, uidvalidity: int,
                    result: AccountResult, lookback_days: int | None,
                    dry_run: bool) -> None:
        """Fissa la posizione di partenza senza scaricare niente."""
        if lookback_days is not None:
            since = self._oggi() - timedelta(days=lookback_days)
            uids = reader.search_uids_since(since)
            last_uid = (min(uids) - 1) if uids else reader.max_uid()
            what = f"da {since.isoformat()} ({len(uids)} messaggi resteranno da scaricare)"
        else:
            last_uid = reader.max_uid()
            what = "posizione attuale, nessuno scaricamento"
        result.examined = 0
        log.info("%s[%s] init: uidvalidity=%s last_uid=%s (%s)",
                 PROVA if dry_run else "", account.id, uidvalidity, last_uid, what)
        if not dry_run:
            cursor = self.state.get_cursor(account.id, account.folder)
            cursor.uidvalidity = uidvalidity
            cursor.last_uid = max(0, last_uid)
            cursor.initialized = True
            cursor.last_seen_at = self.state.ora()
            self.state.save_cursor(cursor)

    def _select_uids(self, account: Account, reader: ImapReader, cursor,
                     uidvalidity: int, mode: str, since: date | None,
                     result: AccountResult, dry_run: bool) -> list[int]:
        if mode == MODE_BACKFILL:
            start = since or (self._oggi() - timedelta(days=365))
            log.info("%s[%s] backfill da %s", PROVA if dry_run else "",
                     account.id, start.isoformat())
            uids = reader.search_uids_since(start)
            return self._filter_known(account, uidvalidity, uids)

        if cursor.uidvalidity is None or not cursor.initialized:
            # Primo avvio prudente: solo i messaggi recenti, mai l'archivio.
            days = account.initial_lookback_days
            if days is None:
                days = self.cfg.initial_lookback_days
            start = self._oggi() - timedelta(days=days)
            log.info("%s[%s] primo avvio: prendo solo da %s (%d giorni). "
                     "Per l'archivio storico usare 'pecfetch backfill'.",
                     PROVA if dry_run else "", account.id, start.isoformat(), days)
            uids = reader.search_uids_since(start)
            return self._filter_known(account, uidvalidity, uids)

        if cursor.uidvalidity != uidvalidity:
            # Il gestore ha ricreato la casella: gli UID memorizzati non valgono
            # più. Si risincronizza per data e la deduplica per contenuto evita
            # di riemettere quello che era già uscito.
            log.warning(
                "%s[%s] UIDVALIDITY cambiata (%s -> %s): risincronizzo per data",
                PROVA if dry_run else "", account.id, cursor.uidvalidity,
                uidvalidity,
            )
            result.uidvalidity_reset = True
            anchor = cursor.last_seen_date
            start = (anchor.date() if anchor else self._oggi()) - timedelta(
                days=self.cfg.resync_overlap_days
            )
            if not dry_run:
                self.state.reset_uidvalidity(account.id, account.folder, uidvalidity)
            uids = reader.search_uids_since(start)
            return self._filter_known(account, uidvalidity, uids)

        return self._filter_known(
            account, uidvalidity, reader.search_uids_above(cursor.last_uid)
        )

    def _filter_known(self, account: Account, uidvalidity: int,
                      uids: list[int]) -> list[int]:
        """Toglie gli UID già conclusi in una esecuzione precedente."""
        out: list[int] = []
        for uid in sorted(uids):
            row = self.state.get_message(account.id, uidvalidity, uid)
            if row is not None and row["status"] in (
                STATUS_DONE, STATUS_SKIPPED, STATUS_DUPLICATE
            ):
                continue
            out.append(uid)
        return out

    # -- un messaggio ----------------------------------------------------
    def _process_message(self, account: Account, reader: ImapReader,
                         uidvalidity: int, uid: int, meta, result: AccountResult) -> None:
        raw = reader.fetch_message(uid)
        digest = content_hash(raw)
        msg_id = message_key(account.id, digest)

        twin = self.state.find_by_hash(account.id, digest)
        # Attenzione: dopo un cambio di UIDVALIDITY gli UID ripartono da capo,
        # quindi il confronto deve tenere conto anche della UIDVALIDITY.
        if twin is not None and (twin["uidvalidity"], twin["uid"]) != (uidvalidity, uid):
            log.info("[%s] UID %s è lo stesso contenuto di UID %s/%s (%s): non riemesso",
                     account.id, uid, twin["uidvalidity"], twin["uid"], msg_id)
            self.state.begin_message(account.id, uidvalidity, uid, msg_id, digest,
                                     twin["msg_type"] or "", twin["message_id"] or "",
                                     twin["subject"] or "")
            self.state.finish_message(account.id, uidvalidity, uid, STATUS_DUPLICATE)
            self.state.advance_uid(account.id, account.folder, uidvalidity, uid)
            result.duplicates += 1
            return

        internaldate = meta.internaldate if meta else None
        pm = parse_pec(raw, internaldate)
        attempts = self.state.begin_message(
            account.id, uidvalidity, uid, msg_id, digest, pm.msg_type,
            pm.message_id, pm.subject,
        )
        seen_at = tempo.reso(certified_or_best_date(pm), self.tz)

        # Ricevute positive: rumore per il consumatore. Si registrano e basta.
        if pm.msg_type in RECEIPTS_POSITIVE or not is_output_type(pm.msg_type):
            self.state.record_receipt(
                msg_id, account.id, pm.msg_type, pm.ref_message_id,
                pm.subject or pm.envelope_subject, pm.gestore,
                tempo.reso(pm.date_certified, self.tz),
            )
            self.state.finish_message(account.id, uidvalidity, uid, STATUS_SKIPPED)
            self.state.advance_uid(account.id, account.folder, uidvalidity, uid, seen_at)
            result.receipts += 1
            log.debug("[%s] UID %s: ricevuta %s registrata", account.id, uid, pm.msg_type)
            return

        row = self.state.get_message(account.id, uidvalidity, uid)
        degraded = attempts > MAX_ATTEMPTS_BEFORE_DEGRADED
        if degraded:
            pm.add_flag("estrazione_saltata_dopo_errori")
            log.warning("[%s] UID %s: %d tentativi falliti, scrivo senza estrazione",
                        account.id, uid, attempts - 1)

        try:
            written = self.writer.write_message(
                pm, raw, account, msg_id, uidvalidity, uid, self.state.ora(),
                extraction=not degraded,
            )
        except Exception as exc:
            self.state.finish_message(account.id, uidvalidity, uid, STATUS_FAILED,
                                      str(exc)[:500])
            raise
        self.state.mark_phase(account.id, uidvalidity, uid, "files",
                              written.content_dir)

        index_file = row["index_file"] if row else None
        if not (row and row["index_done"]):
            index_file = self.writer.append_index(
                written.index_record, certified_or_best_date(pm)
            )
            self.state.mark_phase(account.id, uidvalidity, uid, "index", index_file)

        if not (row and row["archive_done"]):
            try:
                self.archive.add(written.index_record, index_file or "",
                                 written.body_text, written.attachment_text)
                self.state.mark_phase(account.id, uidvalidity, uid, "archive")
            except Exception as exc:
                # L'archivio è memoria storica, non la coda: un suo problema
                # non deve far perdere il messaggio, che è già in output.
                log.error("[%s] archivio non aggiornato per %s: %s",
                          account.id, msg_id, exc)
                if self.run_id is not None:
                    self.state.log_error(self.run_id, account.id, "archive", str(exc))

        self.state.finish_message(account.id, uidvalidity, uid, STATUS_DONE)
        self.state.advance_uid(account.id, account.folder, uidvalidity, uid, seen_at)
        result.written += 1
        log.info("[%s] UID %s -> %s (%s) %s", account.id, uid,
                 written.content_dir, pm.msg_type,
                 "[già presente]" if written.already_present else "")


__all__ = ["Pipeline", "RunSummary", "AccountResult", "MODE_RUN", "MODE_INIT",
           "MODE_BACKFILL", "default_connect"]
