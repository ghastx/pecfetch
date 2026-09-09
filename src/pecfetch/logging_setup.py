"""Logging: leggibile a occhio, senza mai una password dentro."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from datetime import datetime
from pathlib import Path

from . import tempo

_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[-_ ]?key|x-api-key)\b\s*[:=]\s*\S+"
)
_LOGIN_RE = re.compile(r"(?i)\b(LOGIN|AUTHENTICATE)\s+(\S+)\s+(\S+)")
#: le chiavi Anthropic hanno una forma riconoscibile: sparisce il valore,
#: ovunque compaia e comunque ci sia finita.
_APIKEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")


class RedactFilter(logging.Filter):
    """Rete di sicurezza: se una password finisce in un messaggio, sparisce."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _SECRET_RE.sub(r"\1=***", message)
        redacted = _LOGIN_RE.sub(r"\1 \2 ***", redacted)
        redacted = _APIKEY_RE.sub("sk-ant-***", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class _Formatter(logging.Formatter):
    """Timestamp nel fuso dichiarato, con l'offset scritto.

    Il default di `logging` è l'ora locale della macchina, senza dirlo: un log
    letto dopo un cambio di fuso, o su una macchina configurata diversamente,
    non si può più mettere in relazione con le date dei dati.
    """

    def __init__(self, fmt: str, tz) -> None:
        super().__init__(fmt)
        self.tz = tz

    def formatTime(self, record, datefmt=None) -> str:
        when = datetime.fromtimestamp(record.created, self.tz)
        if when.tzinfo is None:      # ripiego: l'ora della macchina, ma dichiarata
            when = when.astimezone()
        return when.strftime(datefmt or "%Y-%m-%d %H:%M:%S%z")


def setup(level: str = "INFO", log_file: Path | None = None,
          quiet: bool = False, verbose: bool = False,
          timezone_name: str = tempo.DEFAULT_TZ) -> logging.Logger:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    numeric = getattr(logging, str(level).upper(), logging.INFO)
    if verbose:
        numeric = logging.DEBUG
    root.setLevel(numeric)

    try:
        tz = tempo.zona(timezone_name)
    except ValueError:
        # il fuso è già validato in configurazione; se qui è sbagliato siamo
        # prima del caricamento e l'ora della macchina è meglio di niente
        tz = None
    fmt = _Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s", tz)
    redact = RedactFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(logging.WARNING if quiet else numeric)
    console.addFilter(redact)
    root.addHandler(console)

    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            fh.setLevel(numeric)
            fh.addFilter(redact)
            root.addHandler(fh)
        except OSError as exc:
            root.warning("impossibile scrivere il log su %s: %s", log_file, exc)

    logging.getLogger("imaplib").setLevel(logging.WARNING)
    return logging.getLogger("pecfetch")


__all__ = ["setup", "RedactFilter"]
