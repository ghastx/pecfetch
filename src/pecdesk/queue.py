"""Lettura della coda di pecfetch, e spostamento di ciò che è stato lavorato.

Due regole, entrambe non negoziabili:

  * i file di input non si modificano mai — si leggono e basta;
  * un percorso letto dentro ``messaggio.json`` è un dato, non un percorso: si
    verifica che resti dentro la cartella del messaggio prima di aprirlo.

La seconda sembra pedanteria — pecfetch sanifica già i nomi — ma il contenuto di
quel JSON deriva da quello che ha spedito un ignoto, e questo programma esiste
proprio perché fra quegli ignoti c'è chi ci prova.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DIR_QUEUE = "coda"
FILE_METADATA = "messaggio.json"
FILE_BODY = "corpo.txt"

#: esiti dell'estrazione che indicano testo potenzialmente sbagliato
OCR_METHODS = frozenset({"pdf_ocr", "image_ocr"})
#: esiti che indicano testo assente: il giudizio dovrà tenerne conto
NO_TEXT_STATUSES = frozenset({
    "empty", "failed", "unsupported", "tool_missing", "disabled", "blocked_type",
    "skipped_too_large", "archive_limit", "encrypted", "unsupported_archive",
})


class QueueError(Exception):
    """La cartella di un messaggio è illeggibile o incoerente."""


@dataclass
class Attachment:
    """Un allegato come lo ha censito pecfetch, più ciò che ne sappiamo noi."""

    name: str
    content_type: str
    size: int
    stored: bool
    method: str
    status: str
    chars: int
    text_path: str = ""
    active: bool = False
    suspicious: bool = False
    note: str = ""
    archive: dict | None = None

    @property
    def is_ocr(self) -> bool:
        return self.method in OCR_METHODS

    @property
    def has_text(self) -> bool:
        return self.chars > 0 and bool(self.text_path)

    @property
    def is_archive(self) -> bool:
        return self.archive is not None or self.status == "unsupported_archive"

    @property
    def extension(self) -> str:
        _, _, ext = (self.name or "").rpartition(".")
        return ext.lower() if ext and "." in (self.name or "") else ""


@dataclass
class QueueItem:
    """Un messaggio in coda. La lettura del testo è pigra: molti non servono."""

    path: Path
    record: dict
    attachments: list[Attachment] = field(default_factory=list)

    # -- identità --------------------------------------------------------
    @property
    def id(self) -> str:
        return str(self.record.get("id", ""))

    @property
    def dir_name(self) -> str:
        return self.path.name

    @property
    def account(self) -> str:
        return str(self.record.get("casella", {}).get("id", ""))

    @property
    def client(self) -> str:
        return str(self.record.get("casella", {}).get("cliente", "") or self.account)

    @property
    def mailbox_label(self) -> str:
        casella = self.record.get("casella", {})
        return str(casella.get("etichetta") or casella.get("indirizzo") or self.account)

    @property
    def mailbox_address(self) -> str:
        return str(self.record.get("casella", {}).get("indirizzo", ""))

    @property
    def msg_type(self) -> str:
        return str(self.record.get("tipo", ""))

    @property
    def certified(self) -> bool:
        return bool(self.record.get("certificato", False))

    @property
    def sender(self) -> str:
        return str(self.record.get("mittente", {}).get("indirizzo", "")).lower()

    @property
    def sender_name(self) -> str:
        return str(self.record.get("mittente", {}).get("nome", ""))

    @property
    def sender_domain(self) -> str:
        return str(self.record.get("mittente", {}).get("dominio", "")).lower()

    @property
    def recipients(self) -> list[str]:
        return [str(a) for a in self.record.get("destinatari", []) if a]

    @property
    def subject(self) -> str:
        return str(self.record.get("oggetto", ""))

    @property
    def date(self) -> str:
        data = self.record.get("data", {})
        return str(data.get("certificata") or data.get("invio")
                   or data.get("ricezione") or self.record.get("acquisito_il", ""))

    @property
    def body_truncated(self) -> bool:
        return bool(self.record.get("contenuto", {}).get("corpo_troncato", False))

    @property
    def notes(self) -> list[str]:
        return [str(n) for n in self.record.get("note", []) or []]

    @property
    def receipt(self) -> dict:
        return dict(self.record.get("ricevuta", {}) or {})

    # -- contenuto -------------------------------------------------------
    def _safe(self, relative: str) -> Path | None:
        """Risolve un percorso dichiarato nei metadati, se resta dentro casa."""
        if not relative:
            return None
        candidate = (self.path / relative).resolve()
        try:
            root = self.path.resolve()
        except OSError:
            return None
        if not candidate.is_relative_to(root):
            return None
        return candidate if candidate.is_file() else None

    def body(self) -> str:
        path = self.path / FILE_BODY
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def attachment_text(self, att: Attachment) -> str:
        path = self._safe(att.text_path)
        if path is None:
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _attachment_from_meta(raw: dict) -> Attachment:
    testo = raw.get("testo", {}) or {}
    return Attachment(
        name=str(raw.get("nome") or raw.get("nome_file") or "allegato"),
        content_type=str(raw.get("content_type", "")),
        size=int(raw.get("byte", 0) or 0),
        stored=bool(raw.get("salvato", False)),
        method=str(testo.get("method", "none")),
        status=str(testo.get("status", "")),
        chars=int(testo.get("chars", 0) or 0),
        text_path=str(testo.get("percorso", "")),
        active=bool(raw.get("contenuto_attivo", False)),
        suspicious=bool(raw.get("contenuto_sospetto", False)),
        note=str(raw.get("nota") or raw.get("motivo") or ""),
        archive=raw.get("archivio"),
    )


def read_item(path: Path) -> QueueItem:
    """Legge la cartella di un messaggio. Non scrive niente, mai."""
    meta_path = Path(path) / FILE_METADATA
    try:
        record = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QueueError(f"{path.name}: metadati non leggibili ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(f"{path.name}: metadati non validi ({exc})") from exc
    if not isinstance(record, dict) or not record.get("id"):
        raise QueueError(f"{path.name}: metadati privi di 'id'")
    attachments = [
        _attachment_from_meta(raw)
        for raw in record.get("allegati", []) or []
        if isinstance(raw, dict)
    ]
    return QueueItem(path=Path(path), record=record, attachments=attachments)


def scan(queue_dir: Path, limit: int | None = None) -> tuple[list[QueueItem], list[str]]:
    """Elenca la coda in ordine di arrivo. Ritorna (elementi, problemi)."""
    problems: list[str] = []
    items: list[QueueItem] = []
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return items, [f"coda non trovata: {queue_dir}"]
    for entry in sorted(queue_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            items.append(read_item(entry))
        except QueueError as exc:
            problems.append(str(exc))
    # il nome della cartella comincia con la data: l'ordine alfabetico è
    # cronologico, ma non ci si appoggia e si riordina sul dato vero
    items.sort(key=lambda it: (it.date or "", it.dir_name))
    if limit is not None:
        items = items[:limit]
    return items, problems


def move_to_worked(item_path: Path, worked_dir: Path,
                   when: datetime | None = None) -> Path:
    """Sposta la cartella fuori dalla coda. Il contenuto resta intatto.

    Il contratto di pecfetch autorizza esplicitamente il consumatore a spostare
    ciò che ha processato: pecfetch non riscrive quello che è già uscito, perché
    la verità su cosa è stato scaricato sta nel suo stato locale.
    """
    item_path = Path(item_path)
    when = when or datetime.now()
    target_dir = Path(worked_dir) / f"{when:%Y-%m-%d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / item_path.name
    if target.exists():
        # già spostato da un'esecuzione interrotta: non si duplica e non si
        # sovrascrive quello che c'è
        if item_path.exists():
            shutil.rmtree(item_path, ignore_errors=True)
        return target
    try:
        os.rename(item_path, target)
    except OSError:
        # filesystem diversi (worked_dir spostata altrove): copia e cancella
        shutil.copytree(item_path, target)
        shutil.rmtree(item_path, ignore_errors=True)
    return target


__all__ = ["Attachment", "QueueItem", "QueueError", "read_item", "scan",
           "move_to_worked", "OCR_METHODS", "NO_TEXT_STATUSES"]
