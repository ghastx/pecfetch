"""Fixture di pecdesk. Le fabbriche stanno in ``fabbrica.py``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from desk.fabbrica import ISTRUZIONI, REGOLE, write_message  # noqa: E402
from pecdesk.config import (ApiSettings, Config, DigestSettings,  # noqa: E402
                            MaterialLimits, SuspicionLimits)
from pecdesk.directives import load_directives  # noqa: E402
from pecdesk.model import Judgment  # noqa: E402
from pecdesk.queue import read_item  # noqa: E402


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    path = tmp_path / "dati" / "coda"
    path.mkdir(parents=True)
    (tmp_path / "dati" / "esiti").mkdir(parents=True)
    return path


@pytest.fixture
def directives(tmp_path: Path):
    rules = tmp_path / "regole.toml"
    instructions = tmp_path / "istruzioni.md"
    rules.write_text(REGOLE, encoding="utf-8")
    instructions.write_text(ISTRUZIONI, encoding="utf-8")
    return load_directives(rules, instructions)


@pytest.fixture
def config(tmp_path: Path, queue_dir: Path) -> Config:
    return Config(
        output_root=tmp_path / "dati",
        archive_path=tmp_path / "archivio.sqlite3",
        state_dir=tmp_path / "stato",
        worked_dir=tmp_path / "dati" / "lavorati",
        rules_path=tmp_path / "regole.toml",
        instructions_path=tmp_path / "istruzioni.md",
        api_key="chiave-finta",
        api_key_source="test",
        model="claude-haiku-4-5",
        material=MaterialLimits(),
        suspicion=SuspicionLimits(),
        api=ApiSettings(),
        digest=DigestSettings(to="titolare@studio.it",
                              sender="pecdesk@studio.it",
                              smtp_host="", send=False,
                              copy_dir=tmp_path / "riepiloghi"),
    )


@pytest.fixture
def item(queue_dir: Path):
    write_message(queue_dir, "msg0001")
    return read_item(next(queue_dir.iterdir()))


@pytest.fixture
def judgment() -> Judgment:
    return Judgment(doc_type="fattura", route="studio",
                    recipient_alias="contabilita", reason="fattura di fornitore",
                    confidence="alta", tokens_in=1200, tokens_out=140)
