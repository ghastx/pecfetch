"""Estrazione del testo dagli allegati.

Requisito, non opzione: su una PEC di lavoro il messaggio è spesso due righe
di accompagnamento e un PDF che contiene tutto. Qui si tira fuori il testo, e
quando l'estrazione diretta torna vuota si ripiega sull'OCR in italiano.

Tre principi:
  * ogni dipendenza esterna è opzionale e caricata lazy: se manca, si annota
    ``tool_missing`` e il messaggio esce lo stesso;
  * ogni strumento esterno gira con un timeout: un PDF malato non blocca il run;
  * l'esito (metodo + stato) finisce nei metadati, così chi legge sa se ha
    davanti testo nativo o un OCR incerto.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .mimeutil import decode_bytes, html_to_text, normalize_text

log = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_TOO_LARGE = "skipped_too_large"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TOOL_MISSING = "tool_missing"
STATUS_DISABLED = "disabled"

_TEXT_EXT = {".txt", ".csv", ".log", ".md", ".asc", ".json", ".ini", ".cfg", ".dat"}
_XML_EXT = {".xml", ".xsd", ".xsl"}
_HTML_EXT = {".html", ".htm", ".xhtml"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}

#: sotto questa soglia di caratteri un PDF si considera scansionato -> OCR
OCR_TRIGGER_CHARS = 32


@dataclass
class ExtractionResult:
    text: str = ""
    method: str = "none"
    status: str = STATUS_UNSUPPORTED
    error: str = ""
    truncated: bool = False
    pages: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)

    def as_dict(self) -> dict:
        out = {"method": self.method, "status": self.status, "chars": self.chars}
        if self.truncated:
            out["truncated"] = True
        if self.pages:
            out["pages"] = self.pages
        if self.error:
            out["error"] = self.error[:300]
        return out


@dataclass
class ExtractorSettings:
    enabled: bool = True
    ocr: bool = True
    ocr_lang: str = "ita"
    ocr_max_pages: int = 20
    ocr_dpi: int = 300
    ocr_timeout: int = 300
    timeout: int = 120
    max_bytes: int = 25 * 1024 * 1024
    max_chars: int = 400_000
    p7m_unwrap: bool = True

    @classmethod
    def from_config(cls, cfg) -> "ExtractorSettings":
        return cls(
            enabled=cfg.extraction_enabled,
            ocr=cfg.ocr_enabled,
            ocr_lang=cfg.ocr_lang,
            ocr_max_pages=cfg.ocr_max_pages,
            ocr_dpi=cfg.ocr_dpi,
            ocr_timeout=cfg.ocr_timeout,
            timeout=cfg.extract_timeout,
            max_bytes=cfg.attachment_max_bytes,
            max_chars=cfg.extract_max_chars,
            p7m_unwrap=cfg.p7m_unwrap,
        )


def _run(cmd: list[str], timeout: int, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(
        cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _optional_import(name: str):
    """Importa una dipendenza opzionale, o None.

    Volutamente ``BaseException``: una libreria compilata installata male (per
    esempio ``cryptography`` con il backend cffi rotto) solleva eccezioni che
    non derivano da ``Exception`` e porterebbe giù l'intero run.
    """
    import importlib

    if name in _IMPORT_CACHE:
        return _IMPORT_CACHE[name]
    module = None
    try:
        module = importlib.import_module(name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        log.debug("dipendenza opzionale %s non utilizzabile: %s", name, exc)
        module = None
    _IMPORT_CACHE[name] = module
    return module


_IMPORT_CACHE: dict[str, object] = {}


# ---------------------------------------------------------------------------
# .p7m  (CAdES): sulle PEC gli atti importanti arrivano quasi sempre firmati.
# Senza sbustare, l'estrazione del testo fallisce proprio sui documenti che
# contano. Il prompt non ne parlava: è stato aggiunto.
# ---------------------------------------------------------------------------

def unwrap_p7m(data: bytes, timeout: int = 60) -> tuple[bytes, str]:
    """Estrae il contenuto firmato da una busta CAdES. Ritorna (dati, errore)."""
    if not _have("openssl"):
        return b"", "openssl non installato"
    last_err = ""
    for form in ("DER", "PEM"):
        for cmd in (
            ["openssl", "cms", "-verify", "-no_signer_cert_verify", "-noverify",
             "-inform", form],
            ["openssl", "smime", "-verify", "-noverify", "-inform", form],
        ):
            try:
                code, out, err = _run(cmd, timeout, stdin=data)
            except (subprocess.TimeoutExpired, OSError) as exc:
                last_err = str(exc)
                continue
            if code == 0 and out:
                return out, ""
            last_err = err.decode("utf-8", "replace").strip()[:200] or f"exit {code}"
    return b"", last_err or "sbustamento p7m fallito"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _pdf_native(data: bytes, settings: ExtractorSettings) -> tuple[str, str, str]:
    """Testo nativo del PDF. Ritorna (testo, metodo, errore)."""
    errors: list[str] = []

    if _have("pdftotext"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
                fh.write(data)
                fh.flush()
                code, out, err = _run(
                    ["pdftotext", "-layout", "-enc", "UTF-8", "-q", fh.name, "-"],
                    settings.timeout,
                )
            if code == 0 and out.strip():
                return decode_bytes(out, "utf-8"), "pdf_pdftotext", ""
            if code != 0:
                errors.append(f"pdftotext: {err.decode('utf-8', 'replace')[:120]}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            errors.append(f"pdftotext: {exc}")

    pypdf = _optional_import("pypdf")
    if pypdf is None:
        errors.append("pypdf assente")
    else:
        try:
            import io

            reader = pypdf.PdfReader(io.BytesIO(data))
            chunks = []
            for page in reader.pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    continue
            text = "\n".join(chunks)
            if text.strip():
                return text, "pdf_pypdf", ""
        except Exception as exc:  # PDF corrotto o cifrato
            errors.append(f"pypdf: {exc}")

    pdfminer = _optional_import("pdfminer.high_level")
    if pdfminer is None:
        errors.append("pdfminer assente")
    else:
        try:
            import io

            text = pdfminer.extract_text(io.BytesIO(data)) or ""
            if text.strip():
                return text, "pdf_pdfminer", ""
        except Exception as exc:
            errors.append(f"pdfminer: {exc}")

    return "", "", "; ".join(errors)


def _pdf_ocr(data: bytes, settings: ExtractorSettings) -> tuple[str, str, int, str]:
    """OCR di un PDF scansionato. Ritorna (testo, metodo, pagine, errore)."""
    if not settings.ocr:
        return "", "", 0, "ocr disabilitato"
    if not (_have("pdftoppm") and _have("tesseract")):
        missing = [t for t in ("pdftoppm", "tesseract") if not _have(t)]
        return "", "", 0, f"strumenti mancanti: {', '.join(missing)}"

    with tempfile.TemporaryDirectory(prefix="pecfetch-ocr-") as tmp:
        pdf_path = Path(tmp) / "in.pdf"
        pdf_path.write_bytes(data)
        try:
            code, _out, err = _run(
                ["pdftoppm", "-r", str(settings.ocr_dpi), "-png",
                 "-f", "1", "-l", str(max(1, settings.ocr_max_pages)),
                 str(pdf_path), str(Path(tmp) / "pg")],
                settings.ocr_timeout,
            )
        except subprocess.TimeoutExpired:
            return "", "", 0, "pdftoppm: timeout"
        except OSError as exc:
            return "", "", 0, f"pdftoppm: {exc}"
        if code != 0:
            return "", "", 0, f"pdftoppm: {err.decode('utf-8', 'replace')[:120]}"

        pages = sorted(Path(tmp).glob("pg*.png"))
        if not pages:
            return "", "", 0, "nessuna pagina renderizzata"

        chunks: list[str] = []
        for page in pages[: settings.ocr_max_pages]:
            text, err_txt = _tesseract(page.read_bytes(), settings)
            if err_txt and not chunks:
                return "", "", 0, err_txt
            chunks.append(text)
        return "\n\n".join(chunks), "pdf_ocr", len(pages), ""


def _tesseract(image: bytes, settings: ExtractorSettings) -> tuple[str, str]:
    if not _have("tesseract"):
        return "", "tesseract non installato"
    try:
        code, out, err = _run(
            ["tesseract", "stdin", "stdout", "-l", settings.ocr_lang, "--psm", "3"],
            settings.ocr_timeout, stdin=image,
        )
    except subprocess.TimeoutExpired:
        return "", "tesseract: timeout"
    except OSError as exc:
        return "", f"tesseract: {exc}"
    if code != 0:
        detail = err.decode("utf-8", "replace")[:160]
        if "Failed loading language" in detail or "traineddata" in detail:
            return "", f"tesseract: lingua '{settings.ocr_lang}' non installata"
        return "", f"tesseract: {detail}"
    return decode_bytes(out, "utf-8"), ""


# ---------------------------------------------------------------------------
# Formati office basati su ZIP: gestiti con la sola standard library.
# ---------------------------------------------------------------------------

_XML_TAG_RE = re.compile(r"<[^>]+>")


def _zip_xml_text(data: bytes, members: list[str], para_tags: tuple[str, ...]) -> str:
    out: list[str] = []
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        for member in members:
            if member.endswith("*"):
                targets = sorted(n for n in names if n.startswith(member[:-1]))
            else:
                targets = [member] if member in names else []
            for target in targets:
                raw = zf.read(target)
                text = decode_bytes(raw, "utf-8")
                for tag in para_tags:
                    text = re.sub(rf"</{tag}>", "\n", text)
                text = _XML_TAG_RE.sub("", text)
                import html as html_mod

                out.append(html_mod.unescape(text))
    return "\n".join(out)


def _docx(data: bytes) -> str:
    return _zip_xml_text(data, ["word/document.xml"], ("w:p", "w:br", "w:tr"))


def _pptx(data: bytes) -> str:
    return _zip_xml_text(data, ["ppt/slides/slide*"], ("a:p", "a:br"))


def _odf(data: bytes) -> str:
    return _zip_xml_text(data, ["content.xml"], ("text:p", "text:h", "table:table-row"))


def _xlsx(data: bytes) -> str:
    """Fogli di calcolo: valori delle celle, un foglio per blocco."""
    import io

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        out: list[str] = []
        for sheet in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            root = ElementTree.fromstring(zf.read(sheet))
            out.append(f"--- {sheet.rsplit('/', 1)[-1]} ---")
            for row in root.iter(f"{ns}row"):
                cells: list[str] = []
                for cell in row.findall(f"{ns}c"):
                    value = cell.find(f"{ns}v")
                    text = value.text if value is not None else ""
                    if cell.get("t") == "s" and text and text.isdigit():
                        idx = int(text)
                        text = shared[idx] if 0 <= idx < len(shared) else ""
                    elif cell.get("t") == "inlineStr":
                        node = cell.find(f"{ns}is")
                        text = "".join(t.text or "" for t in node.iter(f"{ns}t")) if node is not None else ""
                    cells.append((text or "").strip())
                if any(cells):
                    out.append("\t".join(cells))
        return "\n".join(out)


def _eml(data: bytes) -> str:
    """Un .eml allegato: intestazioni utili + corpo, senza ricorsione infinita."""
    from email import message_from_bytes

    from .mimeutil import header

    msg = message_from_bytes(data)
    lines = []
    for name in ("Date", "From", "To", "Subject"):
        value = header(msg, name)
        if value:
            lines.append(f"{name}: {value}")
    from .pec import _collect_body  # riuso interno

    body, _source, _used = _collect_body(msg)
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def extract_text(data: bytes, filename: str, content_type: str,
                 settings: ExtractorSettings, depth: int = 0) -> ExtractionResult:
    """Estrae il testo di un allegato. Non solleva mai."""
    if not settings.enabled:
        return ExtractionResult(status=STATUS_DISABLED, method="none")
    if not data:
        return ExtractionResult(status=STATUS_EMPTY, method="none")
    if len(data) > settings.max_bytes:
        return ExtractionResult(status=STATUS_TOO_LARGE, method="none",
                                error=f"{len(data)} byte oltre il limite")

    ext = _ext(filename)
    ctype = (content_type or "").lower()

    try:
        # Busta di firma digitale: si sbuccia e si riparte sul contenuto.
        if depth < 3 and settings.p7m_unwrap and (
            ext == ".p7m"
            or ctype in ("application/pkcs7-mime", "application/x-pkcs7-mime")
        ):
            inner, err = unwrap_p7m(data, settings.timeout)
            if not inner:
                return ExtractionResult(status=STATUS_FAILED, method="p7m",
                                        error=err or "sbustamento fallito")
            inner_name = filename[: -len(ext)] if ext == ".p7m" else filename
            result = extract_text(inner, inner_name, "", settings, depth + 1)
            result.method = f"p7m+{result.method}"
            return _cap(result, settings)

        if ext == ".pdf" or ctype == "application/pdf" or data[:5] == b"%PDF-":
            text, method, err = _pdf_native(data, settings)
            if len(text.strip()) >= OCR_TRIGGER_CHARS:
                return _cap(ExtractionResult(normalize_text(text), method, STATUS_OK), settings)
            ocr_text, ocr_method, pages, ocr_err = _pdf_ocr(data, settings)
            if ocr_text.strip():
                return _cap(
                    ExtractionResult(normalize_text(ocr_text), ocr_method, STATUS_OK,
                                     pages=pages),
                    settings,
                )
            if text.strip():  # poco testo nativo, ma è tutto quel che c'è
                return _cap(ExtractionResult(normalize_text(text), method, STATUS_OK), settings)
            detail = "; ".join(x for x in (err, ocr_err) if x)
            status = STATUS_TOOL_MISSING if "assente" in detail or "mancanti" in detail else STATUS_EMPTY
            return ExtractionResult(status=status, method=method or "pdf", error=detail)

        if ext in _IMAGE_EXT or ctype.startswith("image/"):
            if not settings.ocr:
                return ExtractionResult(status=STATUS_DISABLED, method="image_ocr")
            text, err = _tesseract(data, settings)
            if text.strip():
                return _cap(ExtractionResult(normalize_text(text), "image_ocr", STATUS_OK), settings)
            status = STATUS_TOOL_MISSING if err else STATUS_EMPTY
            return ExtractionResult(status=status, method="image_ocr", error=err)

        if ext == ".docx" or "wordprocessingml" in ctype:
            return _cap(ExtractionResult(normalize_text(_docx(data)), "docx_xml", STATUS_OK), settings)
        if ext == ".xlsx" or "spreadsheetml" in ctype:
            return _cap(ExtractionResult(normalize_text(_xlsx(data)), "xlsx_xml", STATUS_OK), settings)
        if ext == ".pptx" or "presentationml" in ctype:
            return _cap(ExtractionResult(normalize_text(_pptx(data)), "pptx_xml", STATUS_OK), settings)
        if ext in (".odt", ".ods", ".odp") or ctype.startswith("application/vnd.oasis"):
            return _cap(ExtractionResult(normalize_text(_odf(data)), "odf_xml", STATUS_OK), settings)

        if ext in _HTML_EXT or ctype in ("text/html", "application/xhtml+xml"):
            return _cap(ExtractionResult(html_to_text(decode_bytes(data)), "html", STATUS_OK), settings)
        if ext in _TEXT_EXT or ext in _XML_EXT or ctype.startswith("text/") or ctype in (
            "application/xml", "application/json", "application/csv"
        ):
            return _cap(ExtractionResult(normalize_text(decode_bytes(data)), "plain", STATUS_OK), settings)
        if ext == ".eml" or ctype == "message/rfc822":
            return _cap(ExtractionResult(normalize_text(_eml(data)), "eml", STATUS_OK), settings)
        if ext == ".rtf":
            text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)),
                          decode_bytes(data))
            text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
            text = text.replace("{", " ").replace("}", " ")
            return _cap(ExtractionResult(normalize_text(text), "rtf", STATUS_OK), settings)

        return ExtractionResult(status=STATUS_UNSUPPORTED, method="none",
                                error=f"tipo non gestito: {ctype or ext or '?'}")
    except Exception as exc:  # nessun allegato può far cadere il run
        log.debug("estrazione fallita per %r: %s", filename, exc, exc_info=True)
        return ExtractionResult(status=STATUS_FAILED, method="none", error=str(exc)[:300])


def _cap(result: ExtractionResult, settings: ExtractorSettings) -> ExtractionResult:
    if not result.text.strip():
        result.status = STATUS_EMPTY
        result.text = ""
        return result
    if len(result.text) > settings.max_chars:
        result.text = result.text[: settings.max_chars]
        result.truncated = True
    result.status = STATUS_OK
    return result


def available_tools() -> dict:
    """Diagnostica per `pecfetch check`."""
    tools = {t: _have(t) for t in ("pdftotext", "pdftoppm", "tesseract", "openssl")}
    for mod in ("pypdf", "pdfminer.high_level"):
        tools[mod.split(".")[0]] = _optional_import(mod) is not None
    if tools.get("tesseract"):
        try:
            _code, out, _err = _run(["tesseract", "--list-langs"], 20)
            tools["tesseract_langs"] = sorted(
                l for l in decode_bytes(out).split() if len(l) <= 8
            )[:40]
        except Exception:
            tools["tesseract_langs"] = []
    return tools


__all__ = ["ExtractionResult", "ExtractorSettings", "extract_text",
           "unwrap_p7m", "available_tools", "STATUS_OK", "STATUS_EMPTY"]
