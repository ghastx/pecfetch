"""Riconoscimento e smontaggio delle buste PEC.

Un messaggio su casella PEC è una di queste cose:

  * busta di trasporto (``posta-certificata``): il messaggio vero sta dentro
    ``postacert.eml``; ``daticert.xml`` porta i metadati certificati;
  * busta di anomalia (``X-Trasporto: errore``): mail non certificata
    reimbustata dal gestore, contenuto sempre in ``postacert.eml``;
  * ricevuta (accettazione, presa in carico, avvenuta consegna, mancata
    consegna, non accettazione, errore, virus);
  * qualunque altra cosa.

Qui non si prende nessuna decisione di business: si classifica e si estrae.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import Message
from xml.etree import ElementTree

from .mimeutil import (
    address_list_of,
    decode_bytes,
    header,
    html_to_text,
    iso,
    normalize_text,
    parse_addresses,
    parse_date,
    part_charset,
    part_filename,
    payload_bytes,
)

# ---------------------------------------------------------------------------
# Tassonomia
# ---------------------------------------------------------------------------

TYPE_POSTA_CERTIFICATA = "posta_certificata"
TYPE_BUSTA_ANOMALIA = "busta_anomalia"
TYPE_GENERICO = "generico"

#: tipo daticert / X-Ricevuta -> tipo interno
RECEIPT_TYPES = {
    "accettazione": "accettazione",
    "non-accettazione": "non_accettazione",
    "presa-in-carico": "presa_in_carico",
    "avvenuta-consegna": "avvenuta_consegna",
    "preavviso-errore-consegna": "preavviso_errore_consegna",
    "errore-consegna": "errore_consegna",
    "mancata-consegna": "errore_consegna",
    "rilevazione-virus": "rilevazione_virus",
    "errore-consegna-virus": "rilevazione_virus",
}

#: ricevute che dicono "è andata bene": rumore, si registrano e basta.
RECEIPTS_POSITIVE = {"accettazione", "presa_in_carico", "avvenuta_consegna"}
#: ricevute che dicono "l'invio dello studio è fallito": queste interessano.
RECEIPTS_NEGATIVE = {
    "non_accettazione",
    "errore_consegna",
    "preavviso_errore_consegna",
    "rilevazione_virus",
}
ALL_RECEIPTS = RECEIPTS_POSITIVE | RECEIPTS_NEGATIVE

#: tipi che vanno scritti nella coda di output.
def is_output_type(msg_type: str) -> bool:
    return msg_type not in RECEIPTS_POSITIVE


def receipt_class(msg_type: str) -> str | None:
    if msg_type in RECEIPTS_POSITIVE:
        return "positiva"
    if msg_type in RECEIPTS_NEGATIVE:
        return "negativa"
    return None


# ---------------------------------------------------------------------------
# Strutture
# ---------------------------------------------------------------------------


@dataclass
class Attachment:
    filename: str  # nome originale, mai sanificato
    content_type: str
    size: int
    payload: bytes = field(repr=False, default=b"")
    inline: bool = False
    content_id: str = ""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass
class DatiCert:
    """Contenuto di daticert.xml, per quel che serve a valle."""

    tipo: str = ""
    errore: str = ""
    mittente: str = ""
    destinatari: list[dict] = field(default_factory=list)
    oggetto: str = ""
    gestore: str = ""
    data: datetime | None = None
    identificativo: str = ""
    msgid: str = ""
    ricevuta_tipo: str = ""
    consegna: str = ""
    errore_esteso: str = ""
    raw: bytes = field(repr=False, default=b"")


@dataclass
class ParsedMessage:
    """Il messaggio reale, già liberato dalla busta."""

    msg_type: str = TYPE_GENERICO
    certified: bool = False
    receipt_kind: str | None = None
    receipt_class: str | None = None

    subject: str = ""
    from_addr: dict = field(default_factory=dict)
    to: list[dict] = field(default_factory=list)
    cc: list[dict] = field(default_factory=list)
    reply_to: list[dict] = field(default_factory=list)

    date_sent: datetime | None = None
    date_certified: datetime | None = None
    date_received: datetime | None = None

    message_id: str = ""
    envelope_message_id: str = ""
    ref_message_id: str = ""
    pec_identifier: str = ""
    gestore: str = ""
    error_detail: str = ""

    body_text: str = ""
    body_source: str = ""  # "text" | "html" | "envelope" | ""
    attachments: list[Attachment] = field(default_factory=list)

    daticert: DatiCert | None = None
    envelope_subject: str = ""
    envelope_from: dict = field(default_factory=dict)
    postacert_raw: bytes = field(repr=False, default=b"")
    daticert_raw: bytes = field(repr=False, default=b"")
    flags: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)

    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


# ---------------------------------------------------------------------------
# daticert.xml
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_ZONA_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def _text(node) -> str:
    if node is None:
        return ""
    return normalize_text("".join(node.itertext()))


def parse_daticert(raw: bytes) -> DatiCert | None:
    """daticert.xml -> DatiCert. Ritorna None se non è parsabile."""
    if not raw:
        return None
    dc = DatiCert(raw=raw)
    root = None
    try:
        # Dai bytes: così la dichiarazione <?xml encoding=...?> resta valida.
        root = ElementTree.fromstring(raw.lstrip(b"\xef\xbb\xbf \r\n\t"))
    except Exception:
        try:
            text = decode_bytes(raw, None).lstrip("﻿ \r\n\t")
            # ElementTree rifiuta una str con dichiarazione di encoding.
            text = re.sub(r"^<\?xml[^>]*\?>", "", text).strip()
            root = ElementTree.fromstring(text)
        except Exception:
            return _daticert_fallback(raw, dc)

    dc.tipo = (root.get("tipo") or "").strip().lower()
    dc.errore = (root.get("errore") or "").strip().lower()

    intest = root.find("intestazione")
    if intest is not None:
        dc.mittente = _text(intest.find("mittente"))
        for dest in intest.findall("destinatari"):
            addr = _text(dest)
            if addr:
                dc.destinatari.append(
                    {"address": addr, "tipo": (dest.get("tipo") or "").strip().lower()}
                )
        dc.oggetto = _text(intest.find("oggetto"))

    dati = root.find("dati")
    if dati is not None:
        dc.gestore = _text(dati.find("gestore-emittente"))
        dc.identificativo = _text(dati.find("identificativo"))
        dc.msgid = _text(dati.find("msgid"))
        ric = dati.find("ricevuta")
        if ric is not None:
            dc.ricevuta_tipo = (ric.get("tipo") or "").strip().lower()
        dc.consegna = _text(dati.find("consegna"))
        dc.errore_esteso = _text(dati.find("errore-esteso"))
        dc.data = _parse_daticert_date(dati.find("data"))
    return dc


def _daticert_fallback(raw: bytes, dc: DatiCert) -> DatiCert | None:
    """Ripiego per daticert.xml non ben formati (capita: msgid non escapato).

    Meglio recuperare tipo e mittente a colpi di regex che perdere del tutto i
    metadati certificati del gestore.
    """
    text = decode_bytes(raw, None)
    root_match = re.search(r"<postacert\b([^>]*)>", text)
    if not root_match:
        return None
    attrs = root_match.group(1)
    tipo = re.search(r'tipo\s*=\s*"([^"]*)"', attrs)
    errore = re.search(r'errore\s*=\s*"([^"]*)"', attrs)
    dc.tipo = (tipo.group(1) if tipo else "").strip().lower()
    dc.errore = (errore.group(1) if errore else "").strip().lower()

    def grab(tag: str) -> str:
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S)
        return normalize_text(match.group(1)) if match else ""

    dc.mittente = grab("mittente")
    dc.oggetto = grab("oggetto")
    dc.gestore = grab("gestore-emittente")
    dc.identificativo = grab("identificativo")
    dc.msgid = grab("msgid")
    dc.errore_esteso = grab("errore-esteso")
    for dest in re.finditer(r"<destinatari[^>]*>(.*?)</destinatari>", text, re.S):
        addr = normalize_text(dest.group(1))
        if addr:
            dc.destinatari.append({"address": addr, "tipo": ""})
    giorno, ora = grab("giorno"), grab("ora")
    zona = re.search(r'<data[^>]*zona\s*=\s*"([^"]*)"', text)
    if giorno and ora:
        fake = ElementTree.Element("data")
        if zona:
            fake.set("zona", zona.group(1))
        ElementTree.SubElement(fake, "giorno").text = giorno
        ElementTree.SubElement(fake, "ora").text = ora
        dc.data = _parse_daticert_date(fake)
    return dc if dc.tipo else None


def _parse_daticert_date(node) -> datetime | None:
    if node is None:
        return None
    giorno = _text(node.find("giorno"))
    ora = _text(node.find("ora"))
    zona = (node.get("zona") or "").strip()
    dm = _DATE_RE.match(giorno)
    tm = _TIME_RE.match(ora)
    if not dm or not tm:
        return None
    try:
        day, month, year = (int(x) for x in dm.groups())
        hour, minute = int(tm.group(1)), int(tm.group(2))
        second = int(tm.group(3) or 0)
        tz = timezone.utc
        zm = _ZONA_RE.match(zona)
        if zm:
            sign = 1 if zm.group(1) == "+" else -1
            tz = timezone(sign * timedelta(hours=int(zm.group(2)), minutes=int(zm.group(3))))
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Classificazione
# ---------------------------------------------------------------------------


def classify(msg: Message, dc: DatiCert | None) -> tuple[str, str | None]:
    """Ritorna (msg_type, receipt_kind).

    Priorità: daticert.xml (è il dato certificato del gestore), poi gli header
    X-*, poi euristica sull'oggetto. Non si va oltre: meglio "generico" che una
    classificazione inventata.
    """
    tipo = (dc.tipo if dc else "") or ""
    if tipo in RECEIPT_TYPES:
        kind = RECEIPT_TYPES[tipo]
        return kind, kind
    if tipo == "posta-certificata":
        return TYPE_POSTA_CERTIFICATA, None
    if tipo == "busta-anomalia":
        return TYPE_BUSTA_ANOMALIA, None

    ricevuta = header(msg, "X-Ricevuta").strip().lower()
    if ricevuta in RECEIPT_TYPES:
        kind = RECEIPT_TYPES[ricevuta]
        return kind, kind

    trasporto = header(msg, "X-Trasporto").strip().lower()
    if trasporto == "posta-certificata":
        return TYPE_POSTA_CERTIFICATA, None
    if trasporto == "errore":
        return TYPE_BUSTA_ANOMALIA, None

    # Ultima spiaggia: oggetto normalizzato dalle regole PEC.
    subject = header(msg, "Subject").strip().lower()
    if subject.startswith("anomalia messaggio"):
        return TYPE_BUSTA_ANOMALIA, None
    for prefix, tipo_ric in (
        ("accettazione:", "accettazione"),
        ("consegna:", "avvenuta_consegna"),
        ("avviso di mancata consegna", "errore_consegna"),
        ("avviso di non accettazione", "non_accettazione"),
        ("preavviso di mancata consegna", "preavviso_errore_consegna"),
        ("problema di sicurezza", "rilevazione_virus"),
    ):
        if subject.startswith(prefix):
            return tipo_ric, tipo_ric
    return TYPE_GENERICO, None


# ---------------------------------------------------------------------------
# Smontaggio della busta
# ---------------------------------------------------------------------------


def _walk(msg: Message):
    """walk() difensivo: un sottoalbero rotto non deve buttare giù il resto."""
    try:
        yield msg
        if msg.is_multipart():
            payload = msg.get_payload()
            if isinstance(payload, list):
                for part in payload:
                    if isinstance(part, Message):
                        yield from _walk(part)
    except Exception:
        return


def _is_named(part: Message, name: str) -> bool:
    fname = part_filename(part).strip().lower()
    if fname == name:
        return True
    try:
        param = part.get_param("name")
    except Exception:
        param = None
    if isinstance(param, str) and param.strip().lower() == name:
        return True
    return False


def _content_type(part: Message) -> str:
    try:
        return (part.get_content_type() or "").lower()
    except Exception:
        return "application/octet-stream"


def _find_special_parts(msg: Message) -> tuple[bytes, bytes]:
    """Cerca postacert.eml e daticert.xml ovunque nell'albero."""
    postacert = b""
    daticert = b""
    for part in _walk(msg):
        ctype = _content_type(part)
        if not postacert and (_is_named(part, "postacert.eml") or ctype == "message/rfc822"):
            postacert = _subpart_bytes(part)
        elif not daticert and (
            _is_named(part, "daticert.xml")
            or (ctype in ("application/xml", "text/xml") and _is_named(part, "daticert.xml"))
        ):
            daticert = payload_bytes(part)
    if not daticert:
        # Alcuni gestori spediscono daticert.xml senza nome ma come unico xml.
        xmls = [
            payload_bytes(p)
            for p in _walk(msg)
            if _content_type(p) in ("application/xml", "text/xml")
        ]
        for candidate in xmls:
            if b"<postacert" in candidate[:4096]:
                daticert = candidate
                break
    return postacert, daticert


def _looks_like_message(data: bytes) -> bool:
    head = data[:8192]
    return any(marker in head for marker in
               (b"Content-Type", b"Subject:", b"From:", b"Received:", b"Date:"))


def _decode_transfer(data: bytes, cte: str) -> bytes:
    """Applica a mano un Content-Transfer-Encoding che la libreria ha ignorato.

    Capita con ``Content-Type: message/rfc822`` + ``base64``: formalmente
    illegale, in pratica lo si incontra. Senza questo, postacert.eml verrebbe
    scritto in output come una montagna di base64.
    """
    cte = (cte or "").strip().lower()
    try:
        if cte == "base64":
            import base64

            decoded = base64.b64decode(re.sub(rb"[^A-Za-z0-9+/=]", b"", data))
        elif cte == "quoted-printable":
            import quopri

            decoded = quopri.decodestring(data)
        else:
            return data
    except Exception:
        return data
    return decoded if decoded and _looks_like_message(decoded) else data


def _subpart_bytes(part: Message) -> bytes:
    """Bytes del messaggio incapsulato, sia come message/rfc822 che come blob."""
    try:
        payload = part.get_payload()
    except Exception:
        payload = None
    cte = ""
    try:
        cte = str(part.get("Content-Transfer-Encoding") or "")
    except Exception:
        cte = ""
    # message/rfc822 vero e proprio: la libreria ha già costruito il sottoalbero.
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        try:
            return _decode_transfer(payload[0].as_bytes(), cte)
        except Exception:
            pass
    # Molti gestori spediscono postacert.eml come blob codificato: qui il
    # Content-Transfer-Encoding va decodificato, non ignorato.
    data = payload_bytes(part)
    if data:
        return data
    if isinstance(payload, str):
        return _decode_transfer(payload.encode("utf-8", errors="replace"), cte)
    return b""


def _is_attachment(part: Message, body_parts: set[int]) -> bool:
    if part.is_multipart():
        return False
    if id(part) in body_parts:
        return False
    ctype = _content_type(part)
    if ctype in ("application/pkcs7-signature", "application/x-pkcs7-signature"):
        return False  # smime.p7s: firma della busta, non un allegato utile
    try:
        disp = (part.get_content_disposition() or "").lower()
    except Exception:
        disp = ""
    if disp in ("attachment", "inline"):
        return True
    if part_filename(part):
        return True
    return not ctype.startswith("text/")


def _collect_body(msg: Message) -> tuple[str, str, set[int]]:
    """Ritorna (testo, sorgente, id delle parti usate come corpo).

    Preferisce text/plain; se c'è solo HTML lo converte. L'HTML grezzo non
    finisce mai in output.
    """
    plains: list[tuple[Message, str]] = []
    htmls: list[tuple[Message, str]] = []

    for part in _walk(msg):
        if part.is_multipart():
            continue
        ctype = _content_type(part)
        try:
            disp = (part.get_content_disposition() or "").lower()
        except Exception:
            disp = ""
        if disp == "attachment":
            continue
        if part_filename(part) and ctype not in ("text/plain", "text/html"):
            continue
        raw = payload_bytes(part)
        if not raw:
            continue
        text = decode_bytes(raw, part_charset(part))
        if ctype == "text/plain":
            plains.append((part, text))
        elif ctype == "text/html":
            htmls.append((part, text))

    if plains:
        used = {id(p) for p, _ in plains}
        body = normalize_text("\n\n".join(t for _, t in plains))
        if body:
            return body, "text", used
    if htmls:
        used = {id(p) for p, _ in htmls}
        body = html_to_text("\n".join(t for _, t in htmls))
        if body:
            return body, "html", used
    return "", "", set()


def _collect_attachments(msg: Message, body_parts: set[int]) -> list[Attachment]:
    out: list[Attachment] = []
    for part in _walk(msg):
        if part is msg or part.is_multipart():
            continue
        if not _is_attachment(part, body_parts):
            continue
        ctype = _content_type(part)
        if _is_named(part, "daticert.xml") or _is_named(part, "postacert.eml"):
            continue
        data = _subpart_bytes(part) if ctype == "message/rfc822" else payload_bytes(part)
        name = part_filename(part)
        if not name:
            ext = {
                "application/pdf": ".pdf",
                "text/plain": ".txt",
                "text/html": ".html",
                "message/rfc822": ".eml",
                "image/jpeg": ".jpg",
                "image/png": ".png",
            }.get(ctype, ".bin")
            name = f"allegato-{len(out) + 1}{ext}"
        try:
            disp = (part.get_content_disposition() or "").lower()
        except Exception:
            disp = ""
        cid = header(part, "Content-ID").strip("<>")
        out.append(
            Attachment(
                filename=name,
                content_type=ctype,
                size=len(data),
                payload=data,
                inline=(disp == "inline"),
                content_id=cid,
            )
        )
    return out


_INTERESTING_HEADERS = (
    "Message-ID", "Date", "From", "To", "Cc", "Subject", "Return-Path",
    "X-Trasporto", "X-Ricevuta", "X-TipoRicevuta", "X-Riferimento-Message-ID",
    "X-VerificaSicurezza", "X-Mittente", "X-Destinatari", "X-Riferimento-ID",
)


def _headers_dict(msg: Message) -> dict:
    out: dict[str, str] = {}
    for name in _INTERESTING_HEADERS:
        value = header(msg, name)
        if value:
            out[name] = value
    return out


def parse_pec(raw: bytes, internaldate: datetime | None = None) -> ParsedMessage:
    """Punto d'ingresso: bytes RFC822 -> ParsedMessage. Non solleva mai."""
    pm = ParsedMessage()
    pm.date_received = internaldate
    try:
        envelope = message_from_bytes(raw)
    except Exception:
        pm.add_flag("envelope_unparsable")
        pm.body_text = normalize_text(decode_bytes(raw[:200000], None))
        pm.body_source = "envelope"
        return pm

    pm.headers = _headers_dict(envelope)
    pm.envelope_subject = header(envelope, "Subject")
    env_from = parse_addresses(envelope.get("From"))
    pm.envelope_from = env_from[0] if env_from else {}
    pm.envelope_message_id = header(envelope, "Message-ID").strip()
    pm.ref_message_id = header(envelope, "X-Riferimento-Message-ID").strip()

    postacert_raw, daticert_raw = _find_special_parts(envelope)
    if not postacert_raw.strip():
        postacert_raw = b""   # parte presente ma vuota: vale come assente
    pm.postacert_raw = postacert_raw
    pm.daticert_raw = daticert_raw
    dc = parse_daticert(daticert_raw) if daticert_raw else None
    if daticert_raw and dc is None:
        pm.add_flag("daticert_unparsable")
    pm.daticert = dc

    msg_type, receipt_kind = classify(envelope, dc)
    pm.msg_type = msg_type
    pm.receipt_kind = receipt_kind
    pm.receipt_class = receipt_class(msg_type) if receipt_kind else None
    pm.certified = msg_type not in (TYPE_BUSTA_ANOMALIA, TYPE_GENERICO)

    if dc:
        pm.gestore = dc.gestore
        pm.pec_identifier = dc.identificativo
        pm.date_certified = dc.data
        pm.error_detail = dc.errore_esteso or (
            dc.errore if dc.errore and dc.errore != "nessuno" else ""
        )
        if dc.msgid and not pm.ref_message_id and receipt_kind:
            pm.ref_message_id = dc.msgid

    # Il contenuto reale: dentro postacert.eml per buste e anomalie, nella
    # busta stessa per ricevute e messaggi generici.
    inner: Message | None = None
    if postacert_raw:
        try:
            inner = message_from_bytes(postacert_raw)
        except Exception:
            inner = None
            pm.add_flag("postacert_unparsable")
    elif msg_type in (TYPE_POSTA_CERTIFICATA, TYPE_BUSTA_ANOMALIA):
        pm.add_flag("postacert_missing")

    source = inner if inner is not None else envelope
    pm.subject = header(source, "Subject") or (dc.oggetto if dc else "")
    from_list = address_list_of(source, "From")
    if not from_list and dc and dc.mittente:
        from_list = parse_addresses(dc.mittente)
    pm.from_addr = from_list[0] if from_list else {}
    pm.to = address_list_of(source, "To")
    pm.cc = address_list_of(source, "Cc")
    pm.reply_to = address_list_of(source, "Reply-To")
    if not pm.to and dc and dc.destinatari:
        pm.to = [
            {
                "name": "",
                "address": d["address"],
                "domain": d["address"].rsplit("@", 1)[-1].lower(),
                "tipo": d.get("tipo", ""),
            }
            for d in dc.destinatari
        ]
    pm.date_sent = parse_date(source.get("Date"))
    if pm.date_certified is None:
        pm.date_certified = pm.date_sent or pm.date_received
    pm.message_id = header(source, "Message-ID").strip() or pm.envelope_message_id

    body, body_source, body_parts = _collect_body(source)
    pm.body_text = body
    pm.body_source = body_source
    if not body:
        pm.add_flag("body_empty")
    if inner is not None and not (pm.subject or pm.from_addr or body):
        pm.add_flag("postacert_vuoto")
    pm.attachments = _collect_attachments(source, body_parts)

    if inner is not None:
        pm.headers.update(
            {f"postacert.{k}": v for k, v in _headers_dict(inner).items()}
        )
    return pm


def certified_or_best_date(pm: ParsedMessage) -> datetime:
    for candidate in (pm.date_certified, pm.date_sent, pm.date_received):
        if candidate is not None:
            return candidate
    return datetime.now(timezone.utc)


def summary_line(pm: ParsedMessage) -> str:
    who = pm.from_addr.get("address") or pm.envelope_from.get("address") or "?"
    return f"[{pm.msg_type}] {who} :: {pm.subject[:80]!r}"


__all__ = [
    "Attachment", "DatiCert", "ParsedMessage", "parse_pec", "parse_daticert",
    "classify", "is_output_type", "receipt_class", "certified_or_best_date",
    "summary_line", "RECEIPTS_POSITIVE", "RECEIPTS_NEGATIVE", "ALL_RECEIPTS",
    "TYPE_POSTA_CERTIFICATA", "TYPE_BUSTA_ANOMALIA", "TYPE_GENERICO",
    "iso",
]
