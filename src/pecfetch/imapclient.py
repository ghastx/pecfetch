"""Client IMAP minimale, rigorosamente in sola lettura.

Vincolo di progetto: la casella si apre con EXAMINE e i corpi si leggono con
BODY.PEEK[]. Nessun SELECT, nessuno STORE, nessun EXPUNGE, nessun COPY: sulle
stesse caselle lavora Thunderbird e non deve accorgersi di niente.

Si usa ``imaplib`` della standard library: una dipendenza in meno da mantenere
su una VM di servizio, e controllo diretto su quali comandi vengono emessi.
"""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# Le buste PEC con allegati pesanti superano il limite di default di imaplib.
imaplib._MAXLINE = max(imaplib._MAXLINE, 10_000_000)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)")
_INTERNALDATE_RE = re.compile(rb'\bINTERNALDATE\s+"([^"]+)"')
#: RFC 3501 §9: date-day-fixed ammette il giorno a una cifra riempito con uno
#: spazio (` 5-Sep-2026`), e la zona è sempre ±HHMM.
_INTERNALDATE_PARTS = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})\s+([+-])(\d{2})(\d{2})\s*$"
)


class ImapError(Exception):
    """Qualunque problema con una casella. Non ferma le altre caselle."""


@dataclass(frozen=True)
class MessageMeta:
    uid: int
    size: int
    internaldate: datetime | None


def imap_date(value: date | datetime) -> str:
    """Data nel formato richiesto da SEARCH: 01-Jan-2026."""
    return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year}"


def quote_mailbox(name: str) -> str:
    """Quota (e se serve codifica in UTF-7 modificato) il nome di una cartella."""
    encoded = _encode_utf7(name)
    escaped = encoded.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _encode_utf7(name: str) -> str:
    """UTF-7 modificato (RFC 3501 §5.1.3), solo se necessario."""
    if all(0x20 <= ord(c) <= 0x7E for c in name) and "&" not in name:
        return name
    import base64

    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        raw = "".join(buffer).encode("utf-16-be")
        b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append(f"&{b64}-")
        buffer.clear()

    for ch in name:
        if ch == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append(ch)
        else:
            buffer.append(ch)
    flush()
    return "".join(out)


class ImapReader:
    """Sessione IMAP di sola lettura su una casella."""

    def __init__(self, host: str, port: int, username: str, password: str,
                 use_ssl: bool = True, starttls: bool = False, timeout: int = 60):
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.use_ssl = use_ssl
        self.starttls = starttls
        self.timeout = timeout
        self.conn: imaplib.IMAP4 | None = None
        self.folder: str | None = None
        self.uidvalidity: int | None = None
        self.exists: int = 0

    # -- ciclo di vita ---------------------------------------------------
    def connect(self) -> None:
        try:
            if self.use_ssl:
                context = ssl.create_default_context()
                self.conn = imaplib.IMAP4_SSL(
                    self.host, self.port, ssl_context=context, timeout=self.timeout
                )
            else:
                self.conn = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
                if self.starttls:
                    self.conn.starttls(ssl.create_default_context())
        except (OSError, socket.error, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise ImapError(f"connessione a {self.host}:{self.port} fallita: {exc}") from exc
        try:
            self.conn.login(self.username, self._password)
        except imaplib.IMAP4.error as exc:
            # Il messaggio del server può contenere l'utenza, mai la password.
            raise ImapError(f"login fallito per {self.username}: {_clean(exc)}") from exc

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            # Niente CLOSE: su una casella aperta in EXAMINE è innocuo, ma
            # LOGOUT basta e non lascia dubbi.
            self.conn.logout()
        except Exception:
            try:
                self.conn.shutdown()
            except Exception:
                pass
        finally:
            self.conn = None

    def __enter__(self) -> "ImapReader":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- lettura ---------------------------------------------------------
    def list_folders(self) -> list[str]:
        typ, data = self._cmd("list", '""', "*")
        out: list[str] = []
        for line in data or []:
            if isinstance(line, bytes):
                match = re.search(rb'"([^"]*)"\s*$', line) or re.search(rb"(\S+)\s*$", line)
                if match:
                    out.append(match.group(1).decode("ascii", "replace"))
        return out

    def examine(self, folder: str = "INBOX") -> int:
        """Apre la cartella in SOLA LETTURA e ritorna la UIDVALIDITY."""
        assert self.conn is not None, "connect() non chiamata"
        typ, data = self.conn.select(quote_mailbox(folder), readonly=True)
        if typ != "OK":
            raise ImapError(f"EXAMINE di {folder!r} fallito: {_first(data)}")
        if not self.conn.is_readonly:  # difesa: non deve mai accadere
            raise ImapError("la cartella non è stata aperta in sola lettura")
        self.folder = folder
        raw = self.conn.untagged_responses.get("UIDVALIDITY") or []
        try:
            self.uidvalidity = int(raw[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise ImapError(f"UIDVALIDITY non disponibile per {folder!r}") from exc
        try:
            self.exists = int(_first(data) or 0)
        except (TypeError, ValueError):
            self.exists = 0
        return self.uidvalidity

    def search_uids_above(self, last_uid: int) -> list[int]:
        """UID strettamente maggiori di ``last_uid``.

        ``UID SEARCH UID n:*`` per RFC restituisce comunque l'ultimo messaggio
        anche se il suo UID è minore di n: il filtro lato client non è
        ridondante, è obbligatorio.
        """
        start = max(1, last_uid + 1)
        uids = self._search(f"UID {start}:*")
        return sorted(u for u in uids if u > last_uid)

    def search_uids_since(self, since: date) -> list[int]:
        return sorted(self._search(f"SINCE {imap_date(since)}"))

    def search_all(self) -> list[int]:
        return sorted(self._search("ALL"))

    def max_uid(self) -> int:
        uids = self._search("UID 1:*")
        return max(uids) if uids else 0

    def _search(self, criteria: str) -> list[int]:
        typ, data = self._cmd("uid", "SEARCH", None, criteria)
        if typ != "OK":
            raise ImapError(f"SEARCH {criteria!r} fallita: {_first(data)}")
        out: list[int] = []
        for chunk in data or []:
            if not chunk:
                continue
            for token in chunk.split():
                try:
                    out.append(int(token))
                except ValueError:
                    continue
        return out

    def fetch_meta(self, uids: list[int]) -> dict[int, MessageMeta]:
        """Dimensione e data interna, in blocco: serve a decidere cosa scaricare."""
        out: dict[int, MessageMeta] = {}
        for batch in _batched(uids, 200):
            typ, data = self._cmd(
                "uid", "FETCH", ",".join(str(u) for u in batch),
                "(UID RFC822.SIZE INTERNALDATE)",
            )
            if typ != "OK":
                raise ImapError(f"FETCH metadati fallita: {_first(data)}")
            for line in data or []:
                blob = line[0] if isinstance(line, tuple) else line
                if not isinstance(blob, bytes):
                    continue
                m_uid = _UID_RE.search(blob)
                if not m_uid:
                    continue
                uid = int(m_uid.group(1))
                m_size = _SIZE_RE.search(blob)
                size = int(m_size.group(1)) if m_size else 0
                out[uid] = MessageMeta(uid, size, _parse_internaldate(blob))
        return out

    def fetch_message(self, uid: int) -> bytes:
        """Scarica il messaggio con BODY.PEEK[]: nessun flag viene toccato."""
        typ, data = self._cmd("uid", "FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"FETCH del messaggio UID {uid} fallita: {_first(data)}")
        for line in data or []:
            if isinstance(line, tuple) and len(line) >= 2 and isinstance(line[1], (bytes, bytearray)):
                return bytes(line[1])
        raise ImapError(f"messaggio UID {uid} non restituito dal server")

    def _cmd(self, name: str, *args):
        assert self.conn is not None, "connect() non chiamata"
        method = getattr(self.conn, name)
        try:
            return method(*args)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise ImapError(f"comando {name.upper()} fallito: {_clean(exc)}") from exc


def _parse_internaldate(blob: bytes) -> datetime | None:
    """INTERNALDATE normalizzata a UTC, rispettando l'offset che dichiara.

    Non si passa per ``imaplib.Internaldate2tuple``: quella restituisce uno
    ``struct_time`` in ora **locale della macchina**, e darlo poi a
    ``calendar.timegm`` — che lo interpreta come UTC — riaggiunge l'offset
    locale al risultato. Il valore veniva giusto solo su una VM in UTC, e
    sbagliato di un'ora o due su una in Europe/Rome: due ore di sfasamento su
    un campo che finisce nel nome della cartella, nell'indice e nell'archivio.

    Il mese si risolve con ``_MONTHS`` e non con ``%b`` di ``strptime``, che
    dipende dal locale: con ``LC_TIME`` italiano ``%b`` si aspetta ``set``,
    non ``Sep``, e i nomi dei mesi IMAP sono sempre inglesi.
    """
    match = _INTERNALDATE_RE.search(blob)
    if not match:
        return None
    parts = _INTERNALDATE_PARTS.match(match.group(1).decode("ascii", "replace"))
    if parts is None:
        return None
    day, month, year, hour, minute, second, sign, zone_h, zone_m = parts.groups()
    try:
        offset = timedelta(hours=int(zone_h), minutes=int(zone_m))
        if sign == "-":
            offset = -offset
        declared = datetime(
            int(year), _MONTHS.index(month.capitalize()) + 1, int(day),
            int(hour), int(minute), int(second), tzinfo=timezone(offset),
        )
    except ValueError:
        # mese inesistente, 31 febbraio, offset oltre le 24 ore: la data non si
        # sa, e non saperla è già gestito a valle (`internaldate=None`)
        return None
    return declared.astimezone(timezone.utc)


def _batched(items: list[int], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _first(data) -> str:
    if not data:
        return ""
    item = data[0]
    if isinstance(item, (bytes, bytearray)):
        return item.decode("utf-8", "replace")
    return str(item)


def _clean(exc: Exception) -> str:
    text = str(exc)
    return text.replace("\r", " ").replace("\n", " ")[:500]


__all__ = ["ImapReader", "ImapError", "MessageMeta", "imap_date", "quote_mailbox"]
