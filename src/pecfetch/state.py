"""Stato persistente locale.

Il criterio di "già scaricato" non dipende in alcun modo dai flag IMAP:
l'utente legge la posta da Thunderbird e i flag cambiano sotto i piedi.
La verità sta qui dentro, non nei file di output e non sul server.

Modello:

  * ``mailboxes``  cursore per casella: UIDVALIDITY + ultimo UID lavorato;
  * ``messages``   un record per messaggio visto, con avanzamento a fasi
                   (files -> index -> archive -> done). Le fasi vengono
                   confermate una alla volta: un'interruzione lascia lo stato
                   "indietro", mai "avanti";
  * ``receipts``   registro delle ricevute positive (rumore per il consumatore,
                   ma non si butta via niente);
  * ``runs``/``run_errors`` diario delle esecuzioni.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import permessi as perm, tempo

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"      # ricevuta positiva: registrata, non scritta
STATUS_DUPLICATE = "duplicate"  # stesso contenuto già acquisito su questa casella
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mailboxes (
    account       TEXT NOT NULL,
    folder        TEXT NOT NULL,
    uidvalidity   INTEGER,
    last_uid      INTEGER NOT NULL DEFAULT 0,
    last_seen_at  TEXT,
    initialized   INTEGER NOT NULL DEFAULT 0,
    last_run_at   TEXT,
    last_ok_at    TEXT,
    last_error    TEXT,
    PRIMARY KEY (account, folder)
);

CREATE TABLE IF NOT EXISTS messages (
    account       TEXT NOT NULL,
    uidvalidity   INTEGER NOT NULL,
    uid           INTEGER NOT NULL,
    msg_id        TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    msg_type      TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    files_done    INTEGER NOT NULL DEFAULT 0,
    index_done    INTEGER NOT NULL DEFAULT 0,
    archive_done  INTEGER NOT NULL DEFAULT 0,
    content_dir   TEXT,
    index_file    TEXT,
    message_id    TEXT,
    subject       TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    first_seen_at TEXT NOT NULL,
    done_at       TEXT,
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages (account, content_hash);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages (msg_id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages (status);

CREATE TABLE IF NOT EXISTS receipts (
    msg_id          TEXT PRIMARY KEY,
    account         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    ref_message_id  TEXT,
    subject         TEXT,
    gestore         TEXT,
    date_certified  TEXT,
    recorded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_ref ON receipts (ref_message_id);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    mode          TEXT NOT NULL DEFAULT 'run',
    accounts_ok   INTEGER NOT NULL DEFAULT 0,
    accounts_err  INTEGER NOT NULL DEFAULT 0,
    written       INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    exit_code     INTEGER
);

CREATE TABLE IF NOT EXISTS run_errors (
    run_id   INTEGER NOT NULL,
    account  TEXT,
    phase    TEXT NOT NULL,
    message  TEXT NOT NULL,
    at       TEXT NOT NULL
);
"""


@dataclass
class Cursor:
    """Posizione di lettura su una casella."""

    account: str
    folder: str
    uidvalidity: int | None = None
    last_uid: int = 0
    last_seen_at: str | None = None
    initialized: bool = False

    @property
    def last_seen_date(self) -> datetime | None:
        if not self.last_seen_at:
            return None
        try:
            return datetime.fromisoformat(self.last_seen_at)
        except ValueError:
            return None


class State:
    """Wrapper SQLite. Un solo scrittore per volta (garantito dal lock di run)."""

    def __init__(self, path: str | Path, timezone_name: str = tempo.DEFAULT_TZ,
                 permissions: perm.Permessi | None = None):
        self.path = Path(path)
        self.tz = tempo.zona(timezone_name)
        self.permessi = permissions or perm.Permessi()
        perm.crea_dir(self.path.parent, self.permessi.dir_mode, self.permessi)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(_SCHEMA)
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        perm.applica_sqlite(self.path, self.permessi)

    def ora(self) -> str:
        """Adesso, nel fuso dichiarato: una sola convenzione in tutto il programma."""
        return tempo.ora(self.tz)

    # -- gestione --------------------------------------------------------
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
            yield self.db
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # -- cursori ---------------------------------------------------------
    def get_cursor(self, account: str, folder: str) -> Cursor:
        row = self.db.execute(
            "SELECT * FROM mailboxes WHERE account=? AND folder=?", (account, folder)
        ).fetchone()
        if row is None:
            return Cursor(account=account, folder=folder)
        return Cursor(
            account=account,
            folder=folder,
            uidvalidity=row["uidvalidity"],
            last_uid=row["last_uid"] or 0,
            last_seen_at=row["last_seen_at"],
            initialized=bool(row["initialized"]),
        )

    def save_cursor(self, cur: Cursor) -> None:
        self.db.execute(
            """
            INSERT INTO mailboxes (account, folder, uidvalidity, last_uid,
                                   last_seen_at, initialized)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(account, folder) DO UPDATE SET
                uidvalidity=excluded.uidvalidity,
                last_uid=excluded.last_uid,
                last_seen_at=excluded.last_seen_at,
                initialized=excluded.initialized
            """,
            (
                cur.account, cur.folder, cur.uidvalidity, cur.last_uid,
                cur.last_seen_at, int(cur.initialized),
            ),
        )

    def advance_uid(self, account: str, folder: str, uidvalidity: int, uid: int,
                    seen_at: str | None = None) -> None:
        """Avanza il cursore. Non torna mai indietro."""
        cur = self.get_cursor(account, folder)
        if cur.uidvalidity != uidvalidity:
            cur.uidvalidity = uidvalidity
            cur.last_uid = 0
        cur.last_uid = max(cur.last_uid, uid)
        cur.initialized = True
        if seen_at:
            cur.last_seen_at = tempo.piu_recente(cur.last_seen_at, seen_at)
        self.save_cursor(cur)

    def reset_uidvalidity(self, account: str, folder: str, uidvalidity: int) -> None:
        """UIDVALIDITY cambiata: gli UID memorizzati non valgono più.

        I record dei messaggi restano (servono alla deduplica per contenuto),
        ma il cursore riparte e la risincronizzazione avviene per data.
        """
        cur = self.get_cursor(account, folder)
        cur.uidvalidity = uidvalidity
        cur.last_uid = 0
        self.save_cursor(cur)

    def mark_run(self, account: str, folder: str, ok: bool, error: str | None) -> None:
        now = self.ora()
        self.db.execute(
            """
            INSERT INTO mailboxes (account, folder, last_run_at, last_ok_at, last_error)
            VALUES (?,?,?,?,?)
            ON CONFLICT(account, folder) DO UPDATE SET
                last_run_at=excluded.last_run_at,
                last_ok_at=CASE WHEN ? THEN excluded.last_run_at ELSE mailboxes.last_ok_at END,
                last_error=excluded.last_error
            """,
            (account, folder, now, now if ok else None, error, int(ok)),
        )

    # -- messaggi --------------------------------------------------------
    def find_by_hash(self, account: str, content_hash: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM messages WHERE account=? AND content_hash=? "
            "AND status IN (?,?) LIMIT 1",
            (account, content_hash, STATUS_DONE, STATUS_SKIPPED),
        ).fetchone()

    def get_message(self, account: str, uidvalidity: int, uid: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM messages WHERE account=? AND uidvalidity=? AND uid=?",
            (account, uidvalidity, uid),
        ).fetchone()

    def begin_message(self, account: str, uidvalidity: int, uid: int, msg_id: str,
                      content_hash: str, msg_type: str, message_id: str,
                      subject: str) -> int:
        """Registra il messaggio come 'in lavorazione' PRIMA di scrivere i file.

        Serve a rendere ripartibile un'esecuzione interrotta senza dover
        interrogare la cartella di output (che il consumatore svuota).
        """
        self.db.execute(
            """
            INSERT INTO messages (account, uidvalidity, uid, msg_id, content_hash,
                                  msg_type, status, message_id, subject,
                                  attempts, first_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account, uidvalidity, uid) DO UPDATE SET
                msg_id=excluded.msg_id,
                content_hash=excluded.content_hash,
                msg_type=excluded.msg_type,
                message_id=excluded.message_id,
                subject=excluded.subject,
                attempts=messages.attempts + 1,
                error=NULL
            """,
            (account, uidvalidity, uid, msg_id, content_hash, msg_type,
             STATUS_PENDING, message_id, subject, 1, self.ora()),
        )
        row = self.db.execute(
            "SELECT attempts FROM messages WHERE account=? AND uidvalidity=? AND uid=?",
            (account, uidvalidity, uid),
        ).fetchone()
        return int(row["attempts"]) if row else 1

    def mark_phase(self, account: str, uidvalidity: int, uid: int, phase: str,
                   value: str | None = None) -> None:
        column = {"files": "files_done", "index": "index_done",
                  "archive": "archive_done"}[phase]
        extra = ""
        params: list = [1]
        if phase == "files" and value is not None:
            extra = ", content_dir=?"
            params.append(value)
        elif phase == "index" and value is not None:
            extra = ", index_file=?"
            params.append(value)
        params += [account, uidvalidity, uid]
        self.db.execute(
            f"UPDATE messages SET {column}=?{extra} "
            "WHERE account=? AND uidvalidity=? AND uid=?",
            params,
        )

    def finish_message(self, account: str, uidvalidity: int, uid: int,
                       status: str = STATUS_DONE, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE messages SET status=?, error=?, done_at=? "
            "WHERE account=? AND uidvalidity=? AND uid=?",
            (status, error, self.ora(), account, uidvalidity, uid),
        )

    def record_receipt(self, msg_id: str, account: str, kind: str,
                       ref_message_id: str, subject: str, gestore: str,
                       date_certified: str | None) -> None:
        self.db.execute(
            """
            INSERT INTO receipts (msg_id, account, kind, ref_message_id, subject,
                                  gestore, date_certified, recorded_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(msg_id) DO NOTHING
            """,
            (msg_id, account, kind, ref_message_id, subject, gestore,
             date_certified, self.ora()),
        )

    def pending_messages(self, account: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM messages WHERE status=?"
        params: list = [STATUS_PENDING]
        if account:
            sql += " AND account=?"
            params.append(account)
        return list(self.db.execute(sql + " ORDER BY first_seen_at", params))

    # -- diario ----------------------------------------------------------
    def start_run(self, mode: str) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (started_at, mode) VALUES (?,?)", (self.ora(), mode)
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: int, err: int, written: int,
                   skipped: int, exit_code: int) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, accounts_ok=?, accounts_err=?, "
            "written=?, skipped=?, exit_code=? WHERE id=?",
            (self.ora(), ok, err, written, skipped, exit_code, run_id),
        )

    def log_error(self, run_id: int, account: str | None, phase: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO run_errors (run_id, account, phase, message, at) VALUES (?,?,?,?,?)",
            (run_id, account, phase, message[:4000], self.ora()),
        )

    def stats(self) -> dict:
        def scalar(sql: str, *params):
            row = self.db.execute(sql, params).fetchone()
            return row[0] if row else 0

        return {
            "messages_total": scalar("SELECT COUNT(*) FROM messages"),
            "messages_done": scalar("SELECT COUNT(*) FROM messages WHERE status=?", STATUS_DONE),
            "messages_pending": scalar("SELECT COUNT(*) FROM messages WHERE status=?", STATUS_PENDING),
            "messages_failed": scalar("SELECT COUNT(*) FROM messages WHERE status=?", STATUS_FAILED),
            "receipts": scalar("SELECT COUNT(*) FROM receipts"),
            "runs": scalar("SELECT COUNT(*) FROM runs"),
        }

    def mailbox_rows(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM mailboxes ORDER BY account"))

    def last_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        )

    def run_errors(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM run_errors WHERE run_id=? ORDER BY at", (run_id,))
        )


def content_hash(raw: bytes) -> str:
    """Impronta del contenuto: rete di sicurezza indipendente dagli UID."""
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def message_key(account: str, content_hash_hex: str) -> str:
    """Identificativo stabile del messaggio nell'output (chiave del contratto)."""
    import hashlib

    digest = hashlib.sha256(f"{account}\x00{content_hash_hex}".encode()).hexdigest()
    return digest[:24]


__all__ = [
    "State", "Cursor", "content_hash", "message_key",
    "STATUS_PENDING", "STATUS_DONE", "STATUS_SKIPPED", "STATUS_DUPLICATE",
    "STATUS_FAILED", "SCHEMA_VERSION",
]
