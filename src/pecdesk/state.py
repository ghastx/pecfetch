"""Stato locale di pecdesk. Separato da quello di pecfetch, e per un motivo.

Qui vive l'unica verità su cosa è stato *lavorato*: pecfetch sa cosa è stato
scaricato, pecdesk sa cosa è stato giudicato. Due programmi, due stati, due
modalità di guasto.

L'ordine degli effetti è quello che rende l'idempotenza visibile:

    claim  →  chiamata  →  riga di esito (fsync)  →  classificato  →  rename  →  archiviato

Un'interruzione fra la riga di esito e ``classificato`` viene ricucita alla
ripresa da ``reconcile()``, che rilegge gli esiti scritti e allinea lo stato:
nessuna seconda classificazione, nessun elemento che resta a metà.

Il riepilogo si **prenota prima di partire**: l'effetto si registra prima di
produrlo, quindi al massimo una volta. Un invio interrotto non si ripete da solo.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

#: stati di un messaggio
NEW = "nuovo"
IN_PROGRESS = "in_corso"
CLASSIFIED = "classificato"
ARCHIVED = "archiviato"
SUSPENDED = "sospeso"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    account       TEXT NOT NULL DEFAULT '',
    stato         TEXT NOT NULL,
    tentativi     INTEGER NOT NULL DEFAULT 0,
    ultimo_errore TEXT,
    esito_file    TEXT,
    classe        TEXT,
    claimed_at    TEXT,
    classified_at TEXT,
    archived_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_stato ON messages (stato);

CREATE TABLE IF NOT EXISTS digests (
    giorno       TEXT PRIMARY KEY,
    claimed_at   TEXT NOT NULL,
    sent_at      TEXT,
    destinatario TEXT,
    conteggi     TEXT,
    errore       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    classificati INTEGER NOT NULL DEFAULT 0,
    falliti      INTEGER NOT NULL DEFAULT 0,
    saltati      INTEGER NOT NULL DEFAULT 0,
    nota         TEXT
);
"""


@dataclass
class Claim:
    """Esito di una prenotazione: cosa fare di questo messaggio, adesso."""

    proceed: bool
    reason: str = ""
    attempts: int = 0
    state: str = NEW

    @property
    def needs_move(self) -> bool:
        return self.state == CLASSIFIED


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(_SCHEMA)
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    # -- messaggi ---------------------------------------------------------
    def get(self, msg_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM messages WHERE id=?", (msg_id,)
        ).fetchone()

    def claim(self, msg_id: str, account: str = "",
              max_attempts: int = 3) -> Claim:
        """Prenota un messaggio. Chi è già stato lavorato non si rilavora."""
        row = self.get(msg_id)
        if row is not None:
            state = row["stato"]
            if state == ARCHIVED:
                return Claim(False, "già lavorato", row["tentativi"], state)
            if state == CLASSIFIED:
                # esito già scritto: manca solo lo spostamento
                return Claim(False, "già classificato", row["tentativi"], state)
            if state == SUSPENDED:
                return Claim(False, "sospeso dopo troppi tentativi",
                             row["tentativi"], state)
            if row["tentativi"] >= max_attempts:
                self.suspend(msg_id, "tentativi esauriti")
                return Claim(False, "tentativi esauriti", row["tentativi"], SUSPENDED)
        with self.transaction():
            self.db.execute(
                "INSERT INTO messages (id, account, stato, tentativi, claimed_at) "
                "VALUES (?,?,?,1,?) "
                "ON CONFLICT(id) DO UPDATE SET stato=excluded.stato, "
                "tentativi=messages.tentativi+1, claimed_at=excluded.claimed_at, "
                "account=excluded.account",
                (msg_id, account, IN_PROGRESS, self._now()),
            )
        row = self.get(msg_id)
        return Claim(True, "", row["tentativi"] if row else 1, IN_PROGRESS)

    def mark_classified(self, msg_id: str, outcome_file: str,
                        klass: str = "") -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE messages SET stato=?, esito_file=?, classe=?, "
                "classified_at=?, ultimo_errore=NULL WHERE id=?",
                (CLASSIFIED, outcome_file, klass, self._now(), msg_id),
            )

    def mark_archived(self, msg_id: str) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE messages SET stato=?, archived_at=? WHERE id=?",
                (ARCHIVED, self._now(), msg_id),
            )

    def fail(self, msg_id: str, error: str, max_attempts: int = 3) -> str:
        """Registra un fallimento. L'elemento resta in coda finché ha tentativi."""
        row = self.get(msg_id)
        attempts = int(row["tentativi"]) if row else 1
        state = SUSPENDED if attempts >= max_attempts else NEW
        with self.transaction():
            self.db.execute(
                "UPDATE messages SET stato=?, ultimo_errore=? WHERE id=?",
                (state, error[:500], msg_id),
            )
        return state

    def suspend(self, msg_id: str, reason: str) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE messages SET stato=?, ultimo_errore=? WHERE id=?",
                (SUSPENDED, reason[:500], msg_id),
            )

    def retry(self, msg_id: str) -> bool:
        """Sblocca un messaggio sospeso: azzera i tentativi, non l'esito."""
        row = self.get(msg_id)
        if row is None:
            return False
        with self.transaction():
            self.db.execute(
                "UPDATE messages SET stato=?, tentativi=0, ultimo_errore=NULL "
                "WHERE id=?",
                (NEW, msg_id),
            )
        return True

    def reconcile(self, written_ids) -> list[str]:
        """Allinea lo stato agli esiti effettivamente scritti.

        Un esito su disco è la prova che quel messaggio è stato lavorato, anche
        se il processo è morto prima di scriverlo nello stato. Rilavorarlo
        significherebbe pagarlo due volte e scrivere due esiti per lo stesso
        messaggio: proprio quello che l'idempotenza deve impedire.
        """
        fixed: list[str] = []
        for msg_id in written_ids:
            row = self.get(msg_id)
            if row is None:
                with self.transaction():
                    self.db.execute(
                        "INSERT INTO messages (id, stato, tentativi, classified_at) "
                        "VALUES (?,?,1,?)",
                        (msg_id, CLASSIFIED, self._now()),
                    )
                fixed.append(msg_id)
            elif row["stato"] in (NEW, IN_PROGRESS):
                with self.transaction():
                    self.db.execute(
                        "UPDATE messages SET stato=?, classified_at=COALESCE(classified_at,?) "
                        "WHERE id=?",
                        (CLASSIFIED, self._now(), msg_id),
                    )
                fixed.append(msg_id)
        return fixed

    def classified_not_archived(self) -> list[str]:
        return [r["id"] for r in self.db.execute(
            "SELECT id FROM messages WHERE stato=?", (CLASSIFIED,)
        )]

    def suspended(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM messages WHERE stato=? ORDER BY claimed_at", (SUSPENDED,)
        ))

    # -- riepiloghi -------------------------------------------------------
    def claim_digest(self, day: str, recipient: str) -> bool:
        """Prenota il riepilogo del giorno. Ritorna False se è già stato preso.

        Si registra *prima* di spedire: al massimo una volta. Se l'invio muore a
        metà, la prenotazione resta e il riepilogo non riparte da solo — meglio
        un riepilogo mancato e visibile che due riepiloghi identici.
        """
        try:
            with self.transaction():
                self.db.execute(
                    "INSERT INTO digests (giorno, claimed_at, destinatario) "
                    "VALUES (?,?,?)",
                    (day, self._now(), recipient),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_digest(self, day: str) -> None:
        """Annulla una prenotazione mai diventata invio (guasto prima dell'SMTP)."""
        with self.transaction():
            self.db.execute(
                "DELETE FROM digests WHERE giorno=? AND sent_at IS NULL", (day,)
            )

    def force_digest(self, day: str) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM digests WHERE giorno=?", (day,))

    def mark_digest_sent(self, day: str, counts: str = "") -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE digests SET sent_at=?, conteggi=? WHERE giorno=?",
                (self._now(), counts, day),
            )

    def digest_row(self, day: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM digests WHERE giorno=?", (day,)
        ).fetchone()

    # -- esecuzioni -------------------------------------------------------
    def start_run(self) -> int:
        with self.transaction():
            cur = self.db.execute(
                "INSERT INTO runs (started_at) VALUES (?)", (self._now(),)
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, classified: int, failed: int,
                   skipped: int, note: str = "") -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE runs SET finished_at=?, classificati=?, falliti=?, "
                "saltati=?, nota=? WHERE id=?",
                (self._now(), classified, failed, skipped, note[:500], run_id),
            )

    def last_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ))

    def stats(self) -> dict:
        by_state = {
            r[0]: r[1] for r in self.db.execute(
                "SELECT stato, COUNT(*) FROM messages GROUP BY stato ORDER BY 2 DESC"
            )
        }
        by_class = {
            r[0]: r[1] for r in self.db.execute(
                "SELECT classe, COUNT(*) FROM messages WHERE classe IS NOT NULL "
                "AND classe<>'' GROUP BY classe ORDER BY 2 DESC"
            )
        }
        total = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {"totale": total, "per_stato": by_state, "per_classe": by_class}


__all__ = ["State", "Claim", "NEW", "IN_PROGRESS", "CLASSIFIED", "ARCHIVED",
           "SUSPENDED"]
