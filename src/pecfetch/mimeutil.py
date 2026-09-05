"""Decodifica difensiva di header, charset e corpi MIME.

Regola unica di questo modulo: non solleva mai eccezioni verso il chiamante.
Le PEC italiane sono piene di charset dichiarati male, encoded-word malformati
e Content-Transfer-Encoding rotti; qualunque problema deve degradare in testo
leggibile-alla-meglio, mai fermare il run.
"""

from __future__ import annotations

import email.utils
import html as html_mod
import re
import unicodedata
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser

# Charset dichiarati che non esistono o che non vogliono dire nulla.
_BOGUS_CHARSETS = {
    "",
    "unknown",
    "unknown-8bit",
    "x-unknown",
    "x-user-defined",
    "us-ascii",
    "ansi_x3.110-1983",
    "default",
    "none",
    "null",
    "8bit",
    "binary",
}

# Charset "stretti" che in Italia sono quasi sempre una bugia per un UTF-8.
_LATIN_FAMILY = {
    "iso-8859-1",
    "iso8859-1",
    "latin-1",
    "latin1",
    "iso-8859-15",
    "windows-1252",
    "cp1252",
}

_WS_RE = re.compile(r"[ \t\r\n\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _normalize_charset(charset: str | None) -> str:
    if not charset:
        return ""
    cs = charset.strip().strip("\"'").lower()
    # Aruba & co. ogni tanto emettono 'charset=utf-8"' o 'charset=3Dutf-8'.
    cs = cs.replace("3d", "", 1) if cs.startswith("3d") else cs
    return cs


def decode_bytes(data: bytes, charset: str | None = None) -> str:
    """Decodifica bytes in str provando il charset dichiarato e poi dei ripieghi.

    Euristica specifica: se il charset dichiarato è latin-1/cp1252 ma i byte
    sono UTF-8 validi con almeno un carattere multibyte, vince UTF-8. È il caso
    piu' frequente di mojibake sulle PEC ("Ã¨" al posto di "è").
    """
    if not isinstance(data, (bytes, bytearray)):
        return str(data)
    data = bytes(data)
    cs = _normalize_charset(charset)

    if cs in _LATIN_FAMILY and any(b > 0x7F for b in data):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass

    candidates: list[str] = []
    if cs and cs not in _BOGUS_CHARSETS:
        candidates.append(cs)
    candidates += ["utf-8", "cp1252", "iso-8859-15", "latin-1"]

    for cand in candidates:
        try:
            return data.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace")


def decode_header_value(raw: object) -> str:
    """Decodifica un header RFC 2047, tollerando encoded-word malformati."""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        raw = decode_bytes(bytes(raw), None)
    if not isinstance(raw, str):
        raw = str(raw)
    # Gli a-capo di folding non devono finire nell'output.
    raw = raw.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    try:
        parts = decode_header(raw)
    except Exception:
        return _WS_RE.sub(" ", raw).strip()

    chunks: list[str] = []
    for text, charset in parts:
        if isinstance(text, (bytes, bytearray)):
            chunks.append(decode_bytes(bytes(text), charset))
        else:
            chunks.append(text)
    value = "".join(chunks)
    value = value.replace("\x00", "")
    value = unicodedata.normalize("NFC", value)
    return _WS_RE.sub(" ", value).strip()


def header(msg: Message, name: str, default: str = "") -> str:
    try:
        raw = msg.get(name)
    except Exception:
        return default
    if raw is None:
        return default
    return decode_header_value(raw) or default


def parse_addresses(raw: object) -> list[dict]:
    """Ritorna [{'name':..., 'address':..., 'domain':...}] da un header di indirizzi."""
    value = decode_header_value(raw)
    if not value:
        return []
    try:
        pairs = email.utils.getaddresses([value])
    except Exception:
        pairs = [("", value)]
    out: list[dict] = []
    seen: set[str] = set()
    for name, addr in pairs:
        addr = (addr or "").strip().strip("<>").strip()
        name = (name or "").strip().strip('"').strip()
        if not addr and not name:
            continue
        key = addr.lower() or name.lower()
        if key in seen:
            continue
        seen.add(key)
        domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
        out.append({"name": name, "address": addr, "domain": domain})
    return out


def address_list_of(msg: Message, name: str) -> list[dict]:
    try:
        raws = msg.get_all(name) or []
    except Exception:
        return []
    out: list[dict] = []
    for raw in raws:
        out.extend(parse_addresses(raw))
    return out


def parse_date(raw: object) -> datetime | None:
    """Header Date -> datetime aware, o None se indecifrabile."""
    value = decode_header_value(raw)
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


def payload_bytes(part: Message) -> bytes:
    """Payload decodificato, con ripiego se il Content-Transfer-Encoding è rotto."""
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    if data is None:
        try:
            raw = part.get_payload()
        except Exception:
            return b""
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return b""
    return bytes(data)


def part_charset(part: Message) -> str:
    try:
        return _normalize_charset(part.get_content_charset())
    except Exception:
        return ""


def part_filename(part: Message) -> str:
    """Nome file dell'allegato, decodificando sia RFC 2047 che RFC 2231."""
    for getter in ("get_filename", None):
        try:
            raw = part.get_filename() if getter else part.get_param("name")
        except Exception:
            raw = None
        if raw:
            if isinstance(raw, tuple):  # RFC 2231: (charset, lang, value)
                charset, _lang, value = raw
                try:
                    from urllib.parse import unquote_to_bytes

                    return decode_bytes(unquote_to_bytes(value), charset).strip()
                except Exception:
                    return str(value).strip()
            return decode_header_value(raw).strip()
    return ""


class _TextExtractor(HTMLParser):
    """HTML -> testo leggibile. Volutamente minimale: niente dipendenze."""

    _BLOCK = {
        "p", "div", "br", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4",
        "h5", "h6", "blockquote", "section", "article", "header", "footer",
        "pre", "hr", "form", "fieldset", "dl", "dt", "dd",
    }
    _DROP = {"script", "style", "head", "title", "meta", "link", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._DROP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "li":
            self.parts.append("\n- ")
        elif tag == "td" or tag == "th":
            self.parts.append("\t")
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.lower().startswith(("javascript:", "#", "cid:")):
                self._pending_href = href

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br" and not self._skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._DROP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a":
            href = getattr(self, "_pending_href", "")
            if href:
                self.parts.append(f" <{href}>")
                self._pending_href = ""
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


def html_to_text(html: str) -> str:
    """Converte HTML in testo. Non solleva mai."""
    if not html:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
        text = parser.get_text()
    except Exception:
        # Ripiego brutale: via i tag a colpi di regex.
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html_mod.unescape(text)
    return normalize_text(text)


def normalize_text(text: str) -> str:
    """Normalizza spazi e a-capo mantenendo la struttura a paragrafi."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = text.replace(" ", " ").replace("​", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_NL_RE.sub("\n\n", text)
    try:
        text = unicodedata.normalize("NFC", text)
    except Exception:
        pass
    return text.strip()
