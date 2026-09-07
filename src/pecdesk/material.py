"""Montaggio del materiale da sottoporre al modello: solo l'estratto.

L'obiettivo è la selezione, non la lettura. Per capire *che tipo di documento* è
arrivato bastano quasi sempre mittente, oggetto e prime righe; il resto è spesa e
rischio. Quindi:

  * l'inventario degli allegati va sempre per intero — è gratis, e un ``.zip`` da
    un mittente mai visto si riconosce senza aprire niente;
  * del testo si manda una **testa** (dove stanno intestazione dell'ente,
    protocollo e oggetto) più poche **finestre** attorno alle parole che contano
    per smistare;
  * ogni taglio lascia un marcatore visibile nel testo e una riga nell'esito.

Quest'ultimo punto non è cosmesi: chi legge il riepilogo deve poter vedere che il
giudizio è stato dato su un estratto, e non su ottanta pagine.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

from .config import MaterialLimits
from .queue import Attachment, QueueItem

#: apertura e chiusura del materiale non fidato. Il nonce cambia a ogni
#: esecuzione: il contenuto non può fabbricare un tag di chiusura credibile.
TAG_OPEN = "materiale_non_fidato"

_WS_RUNS = re.compile(r"[ \t ]{2,}")
_NL_RUNS = re.compile(r"\n{3,}")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def new_nonce() -> str:
    return secrets.token_hex(8)


def normalize(text: str) -> str:
    """Riduce il testo a quello che serve, senza cambiarne il senso."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub(" ", text)
    text = _WS_RUNS.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _NL_RUNS.sub("\n\n", text).strip()


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def keyword_spans(text: str, keywords, limits: MaterialLimits,
                  after: int = 0) -> list[tuple[int, int]]:
    """Finestre attorno alle parole che contano, oltre la testa già presa."""
    lowered = text.lower()
    half = max(limits.keyword_window_chars // 2, 40)
    found: list[tuple[int, int]] = []
    for word in keywords:
        start = lowered.find(word, after)
        if start < 0:
            continue
        found.append((max(start - half, after), min(start + len(word) + half, len(text))))
    merged = _merge_spans(found)
    return merged[: max(limits.keyword_windows, 0)]


@dataclass
class Excerpt:
    """L'estratto di un blocco di testo, con quanto è stato lasciato fuori."""

    text: str
    sent: int
    total: int
    windows: int = 0

    @property
    def truncated(self) -> bool:
        return self.sent < self.total


def excerpt(text: str, keywords, limits: MaterialLimits,
            budget: int | None = None) -> Excerpt:
    """Testa più finestre sulle parole chiave, entro il budget dichiarato."""
    text = normalize(text)
    total = len(text)
    budget = limits.attachment_chars if budget is None else budget
    if total <= budget:
        return Excerpt(text=text, sent=total, total=total)

    head_len = min(limits.attachment_head_chars, budget)
    pieces = [(0, head_len)]
    remaining = budget - head_len
    windows = 0
    if remaining > 0:
        for start, end in keyword_spans(text, keywords, limits, after=head_len):
            if remaining <= 0:
                break
            end = min(end, start + remaining)
            if end <= start:
                continue
            pieces.append((start, end))
            remaining -= end - start
            windows += 1

    pieces = _merge_spans(pieces)
    out: list[str] = []
    cursor = 0
    for start, end in pieces:
        if start > cursor:
            out.append(f"\n[…estratto da pecdesk: {start - cursor} caratteri omessi…]\n")
        out.append(text[start:end])
        cursor = end
    if cursor < total:
        out.append(f"\n[…estratto da pecdesk: {total - cursor} caratteri omessi…]")
    sent = sum(end - start for start, end in pieces)
    return Excerpt(text="".join(out), sent=sent, total=total, windows=windows)


@dataclass
class Material:
    """Quello che verrà spedito, e il resoconto di cosa è stato lasciato fuori."""

    nonce: str
    inventory: list[dict] = field(default_factory=list)
    untrusted: str = ""
    #: solo il testo, senza i tag: serve ai segnali euristici
    scan_text: str = ""
    truncations: list[dict] = field(default_factory=list)
    inventory_only: list[str] = field(default_factory=list)
    ocr_attachments: list[str] = field(default_factory=list)
    missing_text: list[str] = field(default_factory=list)
    body_chars: int = 0
    body_truncated: bool = False
    tag_forgery: bool = False

    @property
    def chars(self) -> int:
        return len(self.untrusted)

    def as_dict(self) -> dict:
        """Come finisce nell'esito: il giudizio è stato dato su questo."""
        out: dict = {"corpo_caratteri": self.body_chars,
                     "corpo_troncato": self.body_truncated,
                     "caratteri_inviati": self.chars}
        if self.truncations:
            out["estratti"] = list(self.truncations)
        if self.inventory_only:
            out["allegati_solo_inventario"] = list(self.inventory_only)
        if self.ocr_attachments:
            out["allegati_da_ocr"] = list(self.ocr_attachments)
        if self.missing_text:
            out["allegati_senza_testo"] = list(self.missing_text)
        if self.tag_forgery:
            out["tag_falsificato"] = True
        return out


def _inventory_entry(att: Attachment) -> dict:
    entry = {"nome": att.name, "tipo": att.content_type or "?", "byte": att.size,
             "estrazione": att.status or "?", "metodo": att.method,
             "caratteri_testo": att.chars}
    if att.active:
        entry["contenuto_attivo"] = True
    if att.suspicious:
        entry["contenuto_sospetto"] = True
    if att.is_archive:
        entry["archivio"] = True
        if att.archive:
            entry["voci"] = len(att.archive.get("membri", []) or [])
    if not att.stored:
        entry["salvato"] = False
    if att.note:
        entry["nota"] = att.note[:120]
    return entry


def _interest(att: Attachment, keywords) -> tuple:
    """Ordine di interesse: chi ha testo e parole che contano passa davanti."""
    return (0 if att.has_text else 1, 0 if att.chars else 1, -att.size)


def build(item: QueueItem, limits: MaterialLimits, keywords,
          nonce: str | None = None) -> Material:
    """Monta il materiale non fidato di un messaggio, dentro i budget."""
    mat = Material(nonce=nonce or new_nonce())
    mat.inventory = [_inventory_entry(a) for a in item.attachments]

    blocks: list[str] = []
    used = 0

    subject = normalize(item.subject)[:400]
    blocks.append(f"OGGETTO: {subject or '(nessun oggetto)'}")
    used += len(subject)

    body_raw = normalize(item.body())
    body_ex = excerpt(body_raw, keywords, limits, budget=limits.body_chars)
    mat.body_chars = body_ex.total
    mat.body_truncated = body_ex.truncated or item.body_truncated
    if body_ex.text:
        blocks.append(f"CORPO:\n{body_ex.text}")
        used += body_ex.sent
    else:
        blocks.append("CORPO: (vuoto)")
    if body_ex.truncated:
        mat.truncations.append({"cosa": "corpo", "inviati": body_ex.sent,
                                "totali": body_ex.total})

    ordered = sorted(item.attachments, key=lambda a: _interest(a, keywords))
    chosen = [a for a in ordered if a.has_text][: max(limits.max_attachments, 0)]
    chosen_names = {a.name for a in chosen}

    for att in item.attachments:
        if att.name in chosen_names:
            continue
        mat.inventory_only.append(att.name)
        if not att.has_text:
            mat.missing_text.append(att.name)

    for att in chosen:
        remaining = limits.total_chars - used
        if remaining <= 0:
            mat.inventory_only.append(att.name)
            mat.truncations.append({"cosa": f"allegato:{att.name}", "inviati": 0,
                                    "totali": att.chars, "motivo": "tetto raggiunto"})
            continue
        budget = min(limits.attachment_chars, remaining)
        ex = excerpt(item.attachment_text(att), keywords, limits, budget=budget)
        if not ex.text:
            mat.missing_text.append(att.name)
            continue
        marker = " [TESTO DA OCR: cifre, date, importi e codici possono essere " \
                 "letti male]" if att.is_ocr else ""
        if att.is_ocr:
            mat.ocr_attachments.append(att.name)
        blocks.append(f"ALLEGATO «{att.name}»{marker}:\n{ex.text}")
        used += ex.sent
        if ex.truncated:
            mat.truncations.append({"cosa": f"allegato:{att.name}",
                                    "inviati": ex.sent, "totali": ex.total,
                                    "finestre": ex.windows,
                                    **({"ocr": True} if att.is_ocr else {})})

    body = "\n\n".join(blocks)
    # difesa in profondità: il contenuto non deve poter chiudere il proprio
    # recinto, né riaprirne uno suo
    cleaned = re.sub(rf"(?i)</?\s*{TAG_OPEN}[^>]*>", "[tag rimosso da pecdesk]", body)
    if mat.nonce in cleaned:
        cleaned = cleaned.replace(mat.nonce, "[nonce rimosso da pecdesk]")
    if cleaned != body:
        mat.tag_forgery = True
    mat.scan_text = cleaned
    mat.untrusted = (
        f'<{TAG_OPEN} nonce="{mat.nonce}">\n{cleaned}\n</{TAG_OPEN} nonce="{mat.nonce}">'
    )
    return mat


__all__ = ["Material", "Excerpt", "build", "excerpt", "normalize", "new_nonce",
           "keyword_spans", "TAG_OPEN"]
