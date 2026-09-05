"""Archivio storico interrogabile.

Gli stessi record dell'indice finiscono anche qui. Non è la coda del giorno: è
la memoria di lungo periodo, quella che serve a rispondere a "cosa ci ha
scritto questo ente su questa società negli ultimi due anni". Su file sparsi
non si fa, quindi SQLite con FTS5.

Il file sta di default nella directory di stato locale della VM, NON sulla
condivisione: SQLite e il locking su CIFS/SMB non vanno d'accordo. Se serve
esporlo, si sposta con `[archive].path` sapendo cosa si rischia.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    account         TEXT NOT NULL,
    client          TEXT,
    mailbox_label   TEXT,
    mailbox_address TEXT,
    msg_type        TEXT NOT NULL,
    certified       INTEGER NOT NULL DEFAULT 0,
    receipt_kind    TEXT,
    receipt_class   TEXT,
    ref_message_id  TEXT,
    date_certified  TEXT,
    date_sent       TEXT,
    fetched_at      TEXT NOT NULL,
    from_addr       TEXT,
    from_name       TEXT,
    from_domain     TEXT,
    to_addrs        TEXT,
    subject         TEXT,
    message_id      TEXT,
    pec_identifier  TEXT,
    gestore         TEXT,
    content_dir     TEXT,
    index_file      TEXT,
    n_attachments   INTEGER NOT NULL DEFAULT 0,
    body_chars      INTEGER NOT NULL DEFAULT 0,
    record          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arch_account ON messages (account, date_certified DESC);
CREATE INDEX IF NOT EXISTS idx_arch_from ON messages (from_domain, date_certified DESC);
CREATE INDEX IF NOT EXISTS idx_arch_date ON messages (date_certified DESC);
CREATE INDEX IF NOT EXISTS idx_arch_type ON messages (msg_type);

CREATE TABLE IF NOT EXISTS attachments (
    msg_id       TEXT NOT NULL,
    name         TEXT,
    content_type TEXT,
    size         INTEGER,
    method       TEXT,
    status       TEXT,
    chars        INTEGER,
    PRIMARY KEY (msg_id, name)
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, sender, recipients, body, attachments,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS fts_map (
    msg_id TEXT PRIMARY KEY,
    rowid_fts INTEGER NOT NULL
);
"""


@dataclass
class SearchHit:
    id: str
    account: str
    client: str
    date: str
    msg_type: str
    from_addr: str
    subject: str
    content_dir: str
    snippet: str


class Archive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
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

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def has(self, msg_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM messages WHERE id=?", (msg_id,)
        ).fetchone() is not None

    def add(self, record: dict, index_file: str, body_text: str = "",
            attachment_text: str = "") -> None:
        """Inserisce (o rimpiazza) un record. Idempotente sull'id."""
        msg_id = record["id"]
        mailbox = record.get("casella", {})
        sender = record.get("mittente", {})
        dates = record.get("data", {})
        content = record.get("contenuto", {})
        receipt = record.get("ricevuta", {})
        attachments = record.get("allegati", [])

        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DELETE FROM attachments WHERE msg_id=?", (msg_id,))
            old = self.db.execute(
                "SELECT rowid_fts FROM fts_map WHERE msg_id=?", (msg_id,)
            ).fetchone()
            if old:
                self.db.execute("DELETE FROM messages_fts WHERE rowid=?", (old[0],))
                self.db.execute("DELETE FROM fts_map WHERE msg_id=?", (msg_id,))

            self.db.execute(
                """
                INSERT OR REPLACE INTO messages (
                    id, account, client, mailbox_label, mailbox_address, msg_type,
                    certified, receipt_kind, receipt_class, ref_message_id,
                    date_certified, date_sent, fetched_at, from_addr, from_name,
                    from_domain, to_addrs, subject, message_id, pec_identifier,
                    gestore, content_dir, index_file, n_attachments, body_chars, record
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    msg_id, mailbox.get("id", ""), mailbox.get("cliente", ""),
                    mailbox.get("etichetta", ""), mailbox.get("indirizzo", ""),
                    record.get("tipo", ""), int(bool(record.get("certificato"))),
                    receipt.get("tipo"), receipt.get("classe"),
                    receipt.get("riferimento_message_id"),
                    dates.get("certificata"), dates.get("invio"),
                    record.get("acquisito_il", ""),
                    sender.get("indirizzo", ""), sender.get("nome", ""),
                    sender.get("dominio", ""),
                    ", ".join(record.get("destinatari", [])),
                    record.get("oggetto", ""), record.get("message_id", ""),
                    record.get("identificativo_pec", ""), record.get("gestore", ""),
                    content.get("cartella", ""), index_file,
                    len(attachments), content.get("caratteri_corpo", 0),
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            for att in attachments:
                self.db.execute(
                    "INSERT OR REPLACE INTO attachments "
                    "(msg_id, name, content_type, size, method, status, chars) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (msg_id, att.get("nome", ""), att.get("content_type", ""),
                     att.get("byte", 0), att.get("metodo", ""),
                     att.get("testo", ""), att.get("caratteri", 0)),
                )
            cur = self.db.execute(
                "INSERT INTO messages_fts (subject, sender, recipients, body, attachments) "
                "VALUES (?,?,?,?,?)",
                (
                    record.get("oggetto", ""),
                    f"{sender.get('nome', '')} {sender.get('indirizzo', '')}".strip(),
                    ", ".join(record.get("destinatari", [])),
                    body_text[:200_000],
                    (" ".join(a.get("nome", "") for a in attachments)
                     + "\n" + attachment_text)[:200_000],
                ),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO fts_map (msg_id, rowid_fts) VALUES (?,?)",
                (msg_id, cur.lastrowid),
            )
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # -- interrogazione --------------------------------------------------
    def search(self, query: str = "", client: str = "", account: str = "",
               sender: str = "", since: str = "", until: str = "",
               msg_type: str = "", limit: int = 50) -> list[SearchHit]:
        where: list[str] = []
        params: list = []
        if query:
            where.append(
                "m.id IN (SELECT f.msg_id FROM fts_map f JOIN messages_fts x "
                "ON x.rowid = f.rowid_fts WHERE messages_fts MATCH ?)"
            )
            params.append(query)
        if client:
            where.append("(m.client = ? OR m.account = ?)")
            params += [client, client]
        if account:
            where.append("m.account = ?")
            params.append(account)
        if sender:
            where.append("(m.from_addr LIKE ? OR m.from_domain LIKE ?)")
            params += [f"%{sender}%", f"%{sender}%"]
        if since:
            where.append("COALESCE(m.date_certified, m.fetched_at) >= ?")
            params.append(since)
        if until:
            where.append("COALESCE(m.date_certified, m.fetched_at) <= ?")
            params.append(until + "T23:59:59+99:99" if len(until) == 10 else until)
        if msg_type:
            where.append("m.msg_type = ?")
            params.append(msg_type)

        sql = "SELECT m.* FROM messages m"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(m.date_certified, m.fetched_at) DESC LIMIT ?"
        params.append(limit)

        hits: list[SearchHit] = []
        for row in self.db.execute(sql, params):
            hits.append(
                SearchHit(
                    id=row["id"],
                    account=row["account"],
                    client=row["client"] or row["account"],
                    date=(row["date_certified"] or row["fetched_at"] or "")[:19],
                    msg_type=row["msg_type"],
                    from_addr=row["from_addr"] or "",
                    subject=row["subject"] or "",
                    content_dir=row["content_dir"] or "",
                    snippet=self._snippet(row["id"], query),
                )
            )
        return hits

    def _snippet(self, msg_id: str, query: str) -> str:
        if not query:
            return ""
        row = self.db.execute(
            "SELECT snippet(messages_fts, 3, '', '', ' … ', 16) AS s "
            "FROM messages_fts JOIN fts_map ON fts_map.rowid_fts = messages_fts.rowid "
            "WHERE fts_map.msg_id = ? AND messages_fts MATCH ?",
            (msg_id, query),
        ).fetchone()
        return (row["s"] or "").replace("\n", " ") if row else ""

    def get(self, msg_id: str) -> dict | None:
        row = self.db.execute("SELECT record FROM messages WHERE id=?", (msg_id,)).fetchone()
        return json.loads(row["record"]) if row else None

    def stats(self) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        by_type = {
            r[0]: r[1]
            for r in self.db.execute(
                "SELECT msg_type, COUNT(*) FROM messages GROUP BY msg_type ORDER BY 2 DESC"
            )
        }
        span = self.db.execute(
            "SELECT MIN(COALESCE(date_certified, fetched_at)), "
            "MAX(COALESCE(date_certified, fetched_at)) FROM messages"
        ).fetchone()
        return {"totale": total, "per_tipo": by_type,
                "dal": span[0], "al": span[1]}


__all__ = ["Archive", "SearchHit"]
