"""Fixture comuni: nessun test tocca un server IMAP reale."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pecfetch.archive import Archive  # noqa: E402
from pecfetch.config import Account, Config  # noqa: E402
from pecfetch.extract import ExtractorSettings  # noqa: E402
from pecfetch.imapclient import ImapError, MessageMeta  # noqa: E402
from pecfetch.output import OutputWriter  # noqa: E402
from pecfetch.pipeline import Pipeline  # noqa: E402
from pecfetch.state import State  # noqa: E402


class FakeMailbox:
    """Casella finta: un dizionario uid -> bytes, più la UIDVALIDITY."""

    def __init__(self, uidvalidity: int = 1000):
        self.uidvalidity = uidvalidity
        self.messages: dict[int, bytes] = {}
        self.dates: dict[int, datetime] = {}
        self._next_uid = 1
        self.fail_on_connect: str | None = None
        self.fail_on_examine: str | None = None
        self.fetched: list[int] = []

    def add(self, raw: bytes, when: datetime | None = None, uid: int | None = None) -> int:
        uid = uid if uid is not None else self._next_uid
        self._next_uid = max(self._next_uid, uid + 1)
        self.messages[uid] = raw
        self.dates[uid] = when or datetime.now(timezone.utc)
        return uid

    def renumber(self, new_uidvalidity: int, start: int = 1) -> None:
        """Simula la ricreazione della casella da parte del gestore."""
        old = [self.messages[u] for u in sorted(self.messages)]
        dates = [self.dates[u] for u in sorted(self.messages)]
        self.uidvalidity = new_uidvalidity
        self.messages = {}
        self.dates = {}
        self._next_uid = start
        for raw, when in zip(old, dates):
            self.add(raw, when)


class FakeReader:
    """Implementa la stessa superficie di ImapReader usata dal pipeline."""

    def __init__(self, mailbox: FakeMailbox):
        self.mailbox = mailbox
        self.uidvalidity: int | None = None
        self.exists = 0
        self.closed = False

    def connect(self) -> None:
        if self.mailbox.fail_on_connect:
            raise ImapError(self.mailbox.fail_on_connect)

    def close(self) -> None:
        self.closed = True

    def examine(self, folder: str = "INBOX") -> int:
        if self.mailbox.fail_on_examine:
            raise ImapError(self.mailbox.fail_on_examine)
        self.uidvalidity = self.mailbox.uidvalidity
        self.exists = len(self.mailbox.messages)
        return self.uidvalidity

    def search_uids_above(self, last_uid: int) -> list[int]:
        return sorted(u for u in self.mailbox.messages if u > last_uid)

    def search_uids_since(self, since) -> list[int]:
        return sorted(
            u for u, when in self.mailbox.dates.items() if when.date() >= since
        )

    def search_all(self) -> list[int]:
        return sorted(self.mailbox.messages)

    def max_uid(self) -> int:
        return max(self.mailbox.messages, default=0)

    def fetch_meta(self, uids: list[int]) -> dict[int, MessageMeta]:
        return {
            u: MessageMeta(u, len(self.mailbox.messages[u]), self.mailbox.dates.get(u))
            for u in uids
            if u in self.mailbox.messages
        }

    def fetch_message(self, uid: int) -> bytes:
        self.mailbox.fetched.append(uid)
        if uid not in self.mailbox.messages:
            raise ImapError(f"UID {uid} non trovato")
        return self.mailbox.messages[uid]

    def list_folders(self) -> list[str]:
        return ["INBOX"]


@pytest.fixture
def account() -> Account:
    return Account(
        id="rossi", address="rossi@pec.it", host="imap.example",
        label="Rossi S.r.l.", client_id="ROSSI", username="rossi@pec.it",
        password="segretissima", password_source="test",
    )


@pytest.fixture
def cfg(tmp_path, account) -> Config:
    return Config(
        output_root=tmp_path / "condivisa",
        state_dir=tmp_path / "stato",
        accounts=(account,),
        archive_path=tmp_path / "stato" / "archivio.sqlite3",
        log_file=None,
        initial_lookback_days=7,
        max_messages_per_run=100,
        extraction_enabled=True,
        ocr_enabled=False,          # niente OCR nei test: dipende da tesseract
        # E niente guardia sullo spazio: con la soglia di default l'esito dei
        # test dipenderebbe da quanto spazio ha la macchina di chi li esegue.
        # La guardia si prova dove è il soggetto, in tests/test_spazio.py, con
        # una soglia dichiarata e il filesystem simulato.
        min_free_bytes=0,
    )


@pytest.fixture
def stack(cfg):
    state = State(cfg.state_dir / "stato.sqlite3", cfg.timezone, cfg.permissions)
    archive = Archive(cfg.archive_path, permissions=cfg.permissions)
    writer = OutputWriter(
        cfg.output_root, ExtractorSettings.from_config(cfg),
        body_max_chars=cfg.body_max_chars,
        attachment_store_max_bytes=cfg.attachment_store_max_bytes,
        timezone=cfg.timezone,
        permissions=cfg.permissions,
    )
    yield state, archive, writer
    archive.close()
    state.close()


@pytest.fixture
def make_pipeline(cfg, stack):
    state, archive, writer = stack

    def factory(mailboxes: dict[str, FakeMailbox]):
        def connect(acc):
            return FakeReader(mailboxes[acc.id])

        return Pipeline(cfg, state, writer, archive, connect=connect)

    return factory


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def yesterday(now) -> datetime:
    return now - timedelta(days=1)
