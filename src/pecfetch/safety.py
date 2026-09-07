"""Trattamento dei tipi di file ostili.

`extract` apre per mestiere file che arrivano da mittenti sconosciuti: sulle
caselle PEC girano campagne ricorrenti di finte fatture con allegato compresso,
spedite da caselle certificate compromesse. Qui stanno le regole che decidono
*cosa* si apre, *cosa* si scrive su disco e *con quali limiti*.

Asticella dichiarata: pecfetch non deve reggere un attacco mirato, deve solo non
essere il punto in cui un file malformato manda in stallo il servizio o esce dal
perimetro previsto. E soprattutto: un allegato che non si è potuto trattare
produce un messaggio annotato, non un messaggio mancante.

Niente in questo modulo esegue alcunché: gli archivi si leggono in memoria con
la standard library, i formati office si smontano come ZIP + XML, e non si passa
mai da un interprete o da una suite d'ufficio.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

# ---------------------------------------------------------------------------
# Tipi attivi: mai materializzati sul disco di output.
# ---------------------------------------------------------------------------

#: Estensioni che su una macchina che le incontra sono codice, non documenti.
ACTIVE_EXTENSIONS = frozenset({
    # eseguibili e librerie
    "exe", "com", "scr", "pif", "dll", "sys", "drv", "cpl", "ocx", "efi",
    "msi", "msp", "msix", "appx", "app", "gadget",
    # script
    "bat", "cmd", "ps1", "psm1", "psd1", "vbs", "vbe", "js", "jse", "wsf",
    "wsh", "hta", "sh", "bash", "csh", "ksh", "zsh", "py", "pyc", "pyo",
    "pl", "php", "rb", "lua", "ahk", "scpt",
    # collegamenti e configurazioni che innescano azioni
    "lnk", "url", "scf", "inf", "reg", "job", "appref-ms", "desktop", "service",
    # contenitori che montano o eseguono
    "jar", "class", "iso", "img", "vhd", "vhdx", "vmdk", "dmg", "chm",
})

#: Office con macro: si leggono (sono ZIP+XML, nessuna macro viene eseguita)
#: ma vanno segnalati, perché a valle sono un vettore noto.
MACRO_EXTENSIONS = frozenset({
    "docm", "dotm", "xlsm", "xltm", "xlam", "pptm", "potm", "ppam", "ppsm",
})

#: Impronte iniziali che identificano codice eseguibile a prescindere dal nome.
EXECUTABLE_KINDS = frozenset({"pe", "elf", "macho", "script", "class"})

#: Tipi da cui ha senso ricavare testo: solo questi vengono materializzati
#: quando arrivano dall'interno di un archivio.
EXTRACTABLE_EXTENSIONS = frozenset({
    "pdf", "p7m", "p7s",
    "docx", "docm", "dotx", "dotm",
    "xlsx", "xlsm", "xltx", "xltm",
    "pptx", "pptm", "potx", "potm", "ppsx",
    "odt", "ods", "odp", "rtf", "doc", "xls", "ppt",
    "txt", "csv", "tsv", "log", "md", "asc", "json", "ini", "cfg", "dat",
    "xml", "xsd", "xsl", "html", "htm", "xhtml",
    "png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp",
    "eml", "msg",
    "zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz",
})

#: Archivi che sappiamo aprire con la sola standard library.
ARCHIVE_EXTENSIONS = frozenset({
    "zip", "tar", "gz", "tgz", "bz2", "tbz2", "tb2", "xz", "txz",
})

#: Archivi che NON apriamo. Sono proprio i formati che il malspam predilige per
#: evadere i controlli: non aprirli è la risposta giusta, non una rinuncia.
OPAQUE_ARCHIVE_EXTENSIONS = frozenset({
    "rar", "7z", "arj", "ace", "lzh", "lha", "cab", "z", "alz", "uue",
})

ARCHIVE_CONTENT_TYPES = {
    "application/zip", "application/x-zip-compressed", "application/x-zip",
    "application/gzip", "application/x-gzip", "application/x-tar",
    "application/x-bzip2", "application/x-xz", "application/x-compressed",
}


def suffixes(filename: str) -> list[str]:
    """Tutti i suffissi di un nome, dal primo all'ultimo, senza il punto.

    ``fattura.pdf.exe`` -> ``["pdf", "exe"]``. Si guardano tutti perché un nome
    a doppia estensione è il travestimento più vecchio del mestiere.
    """
    base = os.path.basename((filename or "").strip().replace("\\", "/"))
    parts = [p for p in base.split(".")[1:] if p]
    return [p.strip().lower() for p in parts if len(p) <= 12]


def extension(filename: str) -> str:
    """Ultimo suffisso, quello che decide come il file viene trattato."""
    parts = suffixes(filename)
    return parts[-1] if parts else ""


def is_active_name(filename: str, extra: frozenset[str] | set[str] = frozenset()) -> bool:
    """Vero se il nome dichiara un tipo attivo, in qualunque suffisso."""
    active = ACTIVE_EXTENSIONS | {str(e).lstrip(".").lower() for e in extra}
    return any(suffix in active for suffix in suffixes(filename))


def sniff(data: bytes) -> str:
    """Riconosce il contenuto dai primi byte. Stringa vuota se non lo conosce."""
    if not data:
        return ""
    head = bytes(data[:512])
    if head[:4] == b"%PDF" or b"%PDF-" in head[:1024]:
        return "pdf"
    if head[:2] == b"MZ":
        return "pe"
    if head[:4] == b"\x7fELF":
        return "elf"
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        return "macho"
    if head[:4] == b"\xca\xfe\xba\xbe":
        return "class"
    if head[:2] == b"#!":
        return "script"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"
    if head[:5] == b"{\\rtf":
        return "rtf"
    return ""


@dataclass(frozen=True)
class FileVerdict:
    """Cosa si può fare con un file, prima ancora di guardarci dentro."""

    active: bool = False        # tipo attivo: non si apre e non si scrive
    suspicious: bool = False    # il nome non corrisponde al contenuto
    macro_enabled: bool = False # office con macro: si scrive, ma segnalato
    sniffed: str = ""
    reason: str = ""

    @property
    def storable(self) -> bool:
        return not self.active


def classify_file(filename: str, content_type: str = "", data: bytes = b"",
                  extra_active: frozenset[str] | set[str] = frozenset()) -> FileVerdict:
    """Verdetto unico usato sia dall'estrazione sia dalla scrittura.

    Vale il contenuto, non l'estensione: un ``fattura.pdf`` che comincia per
    ``MZ`` è un eseguibile travestito e viene trattato come tale.
    """
    ext = extension(filename)
    sniffed = sniff(data)
    ctype = (content_type or "").lower()

    if is_active_name(filename, extra_active):
        return FileVerdict(active=True, sniffed=sniffed,
                           reason=f"estensione attiva: .{ext or '?'}")
    if sniffed in EXECUTABLE_KINDS:
        return FileVerdict(active=True, suspicious=True, sniffed=sniffed,
                           reason=f"contenuto eseguibile ({sniffed}) sotto nome .{ext or '?'}")
    if ctype in ("application/x-msdownload", "application/x-executable",
                 "application/vnd.microsoft.portable-executable"):
        return FileVerdict(active=True, sniffed=sniffed,
                           reason=f"content type eseguibile: {ctype}")

    suspicious = bool(sniffed) and _mismatch(ext, sniffed)
    return FileVerdict(
        macro_enabled=ext in MACRO_EXTENSIONS,
        suspicious=suspicious,
        sniffed=sniffed,
        reason=f"contenuto {sniffed} sotto nome .{ext}" if suspicious else "",
    )


_EXPECTED_KINDS = {
    "pdf": {"pdf"}, "p7m": {"", "ole"}, "rtf": {"rtf", ""},
    "zip": {"zip"}, "gz": {"gz"}, "tgz": {"gz"}, "bz2": {"bz2"}, "xz": {"xz"},
    "rar": {"rar"}, "7z": {"7z"},
    "doc": {"ole"}, "xls": {"ole"}, "ppt": {"ole"}, "msg": {"ole"},
}
_ZIP_BASED = {"docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm",
              "pptx", "pptm", "potx", "potm", "ppsx", "odt", "ods", "odp"}


def _mismatch(ext: str, sniffed: str) -> bool:
    if not ext or not sniffed:
        return False
    if ext in _ZIP_BASED:
        return sniffed != "zip"
    expected = _EXPECTED_KINDS.get(ext)
    return expected is not None and sniffed not in expected


def is_extractable(filename: str) -> bool:
    """Vero se da questo tipo ha senso provare a ricavare testo."""
    return extension(filename) in EXTRACTABLE_EXTENSIONS


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------

_DTD_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
MAX_XML_BYTES = 16 * 1024 * 1024


class UnsafeXML(ValueError):
    """XML che dichiara entità o DTD: non lo si dà in pasto al parser."""


def safe_xml_fromstring(data: bytes, max_bytes: int = MAX_XML_BYTES):
    """``ElementTree.fromstring`` con le dichiarazioni di entità rifiutate.

    Le versioni recenti di expat limitano già l'amplificazione da entità, ma è
    una protezione che dipende dalla versione installata sul sistema e non ci si
    vuole appoggiare. Né ``daticert.xml`` né i formati OOXML hanno mai un DTD:
    rifiutarlo non toglie niente di legittimo.
    """
    if not isinstance(data, (bytes, bytearray)):
        data = str(data).encode("utf-8", "replace")
    data = bytes(data)
    if len(data) > max_bytes:
        raise UnsafeXML(f"XML di {len(data)} byte oltre il limite di {max_bytes}")
    if _DTD_RE.search(data):
        raise UnsafeXML("dichiarazione DTD o di entità rifiutata")
    return ElementTree.fromstring(data)


# ---------------------------------------------------------------------------
# Archivi
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchiveLimits:
    """Limiti espliciti e configurabili sull'apertura degli archivi."""

    enabled: bool = True
    max_ratio: int = 120                       # rapporto di espansione
    max_total_bytes: int = 64 * 1024 * 1024    # totale espanso
    max_entries: int = 500                     # numero di voci
    max_depth: int = 3                         # annidamento
    max_member_bytes: int = 32 * 1024 * 1024   # singola voce

    def budget(self, source_bytes: int) -> "ArchiveBudget":
        return ArchiveBudget(limits=self, source_bytes=max(1, source_bytes))


@dataclass
class ArchiveBudget:
    """Contatori condivisi da TUTTO l'albero di un archivio.

    Deliberatamente non per livello: con un budget per livello tre archivi
    annidati da 500 voci farebbero 125 milioni di voci.
    """

    limits: ArchiveLimits
    source_bytes: int = 1
    entries: int = 0
    expanded: int = 0
    stopped: str = ""

    @property
    def ratio(self) -> float:
        return round(self.expanded / max(1, self.source_bytes), 1)

    def stop(self, reason: str) -> None:
        if not self.stopped:
            self.stopped = reason

    def can_add_entry(self) -> bool:
        if self.entries >= self.limits.max_entries:
            self.stop(f"oltre {self.limits.max_entries} voci")
            return False
        return True

    def account(self, size: int) -> bool:
        """Registra i byte espansi. Falso se un limite è stato superato."""
        self.expanded += size
        if self.expanded > self.limits.max_total_bytes:
            self.stop(f"oltre {self.limits.max_total_bytes} byte espansi")
            return False
        if self.expanded / max(1, self.source_bytes) > self.limits.max_ratio:
            self.stop(f"rapporto di espansione oltre {self.limits.max_ratio}:1")
            return False
        return True


@dataclass
class ArchiveMember:
    declared_path: str          # com'è scritto DENTRO l'archivio: solo un dato
    name: str                   # nome base, usato per decidere il trattamento
    data: bytes = field(repr=False, default=b"")
    size: int = 0
    skipped: str = ""           # motivo, se non è stato letto


@dataclass
class ArchiveReport:
    format: str = ""
    entries: int = 0
    expanded_bytes: int = 0
    ratio: float = 0.0
    stopped: str = ""
    encrypted: bool = False
    error: str = ""

    def as_dict(self) -> dict:
        out = {"formato": self.format, "voci": self.entries,
               "espanso_byte": self.expanded_bytes, "rapporto": self.ratio}
        if self.stopped:
            out["limite_superato"] = self.stopped
        if self.encrypted:
            out["cifrato"] = True
        if self.error:
            out["errore"] = self.error[:300]
        return out


def archive_kind(filename: str, data: bytes) -> str:
    """Che archivio è: 'zip', 'tar', 'gz', 'bz2', 'xz', 'opaque' o ''."""
    ext = extension(filename)
    sniffed = sniff(data)
    if ext in OPAQUE_ARCHIVE_EXTENSIONS or sniffed in ("rar", "7z"):
        return "opaque"
    if sniffed == "zip" or ext == "zip":
        # tarfile non c'entra: uno ZIP è uno ZIP anche se si chiama .tar
        return "zip"
    if ext in ("tar",) or (data[257:262] == b"ustar"):
        return "tar"
    if sniffed == "gz" or ext in ("gz", "tgz"):
        return "gz"
    if sniffed == "bz2" or ext in ("bz2", "tbz2", "tb2"):
        return "bz2"
    if sniffed == "xz" or ext in ("xz", "txz"):
        return "xz"
    return ""


def _basename(declared: str) -> str:
    """Solo l'ultimo segmento: il percorso dichiarato non si usa mai come path."""
    cleaned = (declared or "").replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or "voce"


def _bounded_read(fh, budget: ArchiveBudget, member_limit: int) -> tuple[bytes, str]:
    """Legge a blocchi fermandosi ai limiti.

    Si legge davvero a blocchi invece di fidarsi di ``file_size``: quell'header
    lo dichiara chi ha costruito l'archivio.
    """
    chunks: list[bytes] = []
    size = 0
    while True:
        block = fh.read(65536)
        if not block:
            break
        size += len(block)
        if size > member_limit:
            return b"", f"voce oltre {member_limit} byte"
        if not budget.account(len(block)):
            return b"", budget.stopped
        chunks.append(block)
    return b"".join(chunks), ""


def read_archive(data: bytes, filename: str, budget: ArchiveBudget,
                 depth: int = 0) -> tuple[list[ArchiveMember], ArchiveReport]:
    """Legge le voci di un archivio applicando i limiti. Non solleva mai.

    ``budget`` è condiviso da tutto l'albero; ``depth`` è il livello di
    annidamento di *questo* archivio.
    """
    kind = archive_kind(filename, data)
    report = ArchiveReport(format=kind)
    if kind == "opaque":
        report.error = "formato non aperto per scelta"
        return [], report
    if not kind:
        report.error = "formato non riconosciuto"
        return [], report
    if depth >= budget.limits.max_depth:
        budget.stop(f"annidamento oltre {budget.limits.max_depth} livelli")
        report.stopped = budget.stopped
        return [], report

    try:
        if kind == "zip":
            members = _read_zip(data, budget, report)
        elif kind in ("gz", "bz2", "xz"):
            members = _read_stream(data, filename, kind, budget, report)
        else:
            members = _read_tar(data, budget, report)
    except Exception as exc:            # archivio corrotto o troncato
        report.error = f"{type(exc).__name__}: {exc}"[:300]
        members = []

    report.entries = len(members)
    report.expanded_bytes = budget.expanded
    report.ratio = budget.ratio
    report.stopped = report.stopped or budget.stopped
    return members, report


def _read_zip(data: bytes, budget: ArchiveBudget,
              report: ArchiveReport) -> list[ArchiveMember]:
    out: list[ArchiveMember] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if budget.stopped:
                break
            if info.is_dir():
                continue
            if not budget.can_add_entry():
                break
            budget.entries += 1
            name = _basename(info.filename)
            if info.flag_bits & 0x1:
                report.encrypted = True
                out.append(ArchiveMember(info.filename, name,
                                         skipped="voce cifrata, nessun tentativo di password"))
                continue
            try:
                with zf.open(info) as fh:
                    payload, why = _bounded_read(fh, budget,
                                                 budget.limits.max_member_bytes)
            except Exception as exc:
                out.append(ArchiveMember(info.filename, name,
                                         skipped=f"lettura fallita: {exc}"[:200]))
                continue
            out.append(ArchiveMember(info.filename, name, payload,
                                     len(payload), why))
    return out


def _read_tar(data: bytes, budget: ArchiveBudget,
              report: ArchiveReport) -> list[ArchiveMember]:
    out: list[ArchiveMember] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for member in tf:
            if budget.stopped:
                break
            if member.isdir():
                continue
            if not member.isreg():
                # symlink, hardlink, device, fifo: censiti e mai materializzati
                out.append(ArchiveMember(
                    member.name, _basename(member.name),
                    skipped=f"voce non regolare ({_tar_kind(member)})"))
                continue
            if not budget.can_add_entry():
                break
            budget.entries += 1
            fh = tf.extractfile(member)
            if fh is None:
                out.append(ArchiveMember(member.name, _basename(member.name),
                                         skipped="voce illeggibile"))
                continue
            with fh:
                payload, why = _bounded_read(fh, budget,
                                             budget.limits.max_member_bytes)
            out.append(ArchiveMember(member.name, _basename(member.name),
                                     payload, len(payload), why))
    return out


def _tar_kind(member) -> str:
    for attr, label in (("issym", "symlink"), ("islnk", "hardlink"),
                        ("ischr", "device"), ("isblk", "device"),
                        ("isfifo", "fifo")):
        if getattr(member, attr)():
            return label
    return "sconosciuta"


def _read_stream(data: bytes, filename: str, kind: str, budget: ArchiveBudget,
                 report: ArchiveReport) -> list[ArchiveMember]:
    """gz / bz2 / xz singoli: una sola voce, che può essere a sua volta un tar."""
    if not budget.can_add_entry():
        return []
    budget.entries += 1
    stream = io.BytesIO(data)
    if kind == "gz":
        fh = gzip.GzipFile(fileobj=stream)
    elif kind == "bz2":
        fh = bz2.BZ2File(stream)
    else:
        fh = lzma.LZMAFile(stream)
    with fh:
        payload, why = _bounded_read(fh, budget, budget.limits.max_member_bytes)

    base = _basename(filename)
    for suffix in (".gz", ".tgz", ".bz2", ".tbz2", ".tb2", ".xz", ".txz"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            if suffix in (".tgz", ".tbz2", ".txz"):
                base += ".tar"
            break
    return [ArchiveMember(base, base or "contenuto", payload, len(payload), why)]


def open_office_zip(data: bytes, limits: ArchiveLimits) -> tuple[dict[str, bytes], str]:
    """Membri di un file office, indicizzati per percorso interno.

    I formati office SONO archivi ZIP: la bomba di decompressione è la stessa e
    la guardia dev'essere la stessa. Qui serve il percorso interno completo
    (``word/document.xml``), che però viene usato solo come chiave di un
    dizionario, mai come percorso su disco.
    """
    budget = limits.budget(len(data))
    members, report = read_archive(data, "contenitore.zip", budget)
    return ({m.declared_path: m.data for m in members if m.data},
            report.stopped or report.error)


__all__ = [
    "ACTIVE_EXTENSIONS", "MACRO_EXTENSIONS", "EXTRACTABLE_EXTENSIONS",
    "ARCHIVE_EXTENSIONS", "OPAQUE_ARCHIVE_EXTENSIONS", "ARCHIVE_CONTENT_TYPES",
    "ArchiveLimits", "ArchiveBudget", "ArchiveMember", "ArchiveReport",
    "FileVerdict", "UnsafeXML",
    "archive_kind", "classify_file", "extension", "is_active_name",
    "is_extractable", "read_archive", "safe_xml_fromstring", "sniff", "suffixes",
]
