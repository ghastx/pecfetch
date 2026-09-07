"""Sanificazione dei nomi di file e cartelle.

I nomi degli allegati arrivano dal mittente, quindi non sono un dato di cui
fidarsi: qui diventano un singolo componente di percorso innocuo, che non può
uscire dalla cartella del messaggio. Le regole tengono anche i nomi problematici
su Windows, che non costano nulla e servono se un domani la cartella viene
esposta. Il nome originale non viene mai perso: viaggia nei metadati.
"""

from __future__ import annotations

import re
import unicodedata

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
MAX_COMPONENT = 96


def slugify(value: str, fallback: str = "x") -> str:
    """Slug ASCII per identificatori tecnici (nomi cartella, id casella)."""
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    value = re.sub(r"-{2,}", "-", value)
    return (value or fallback).lower()[:MAX_COMPONENT]


def safe_filename(name: str, fallback: str = "allegato") -> str:
    """Sanifica un nome file conservando estensione e accenti.

    Via i separatori di percorso, i caratteri di controllo, i punti e spazi
    finali e i nomi riservati DOS: il risultato è sempre un solo componente.
    """
    name = (name or "").strip()
    name = name.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    # I separatori di percorso diventano underscore invece di tagliare il nome:
    # "Fattura 1/2026.pdf" deve restare leggibile, non diventare "2026.pdf".
    # La traversal resta impossibile perché il risultato è un solo componente.
    try:
        name = unicodedata.normalize("NFC", name)
    except Exception:
        pass
    name = _FORBIDDEN.sub("_", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    name = name.rstrip(". ")
    if not name or set(name) <= {"."}:
        return fallback

    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 12 or not stem:
        stem, ext = name, ""
    ext = re.sub(r"\s+", "", ext)

    if stem.lower() in _RESERVED:
        stem = f"_{stem}"

    budget = MAX_COMPONENT - (len(ext) + 1 if ext else 0)
    if budget < 8:
        budget = 8
    if len(stem) > budget:
        stem = stem[:budget].rstrip(". ")
    out = f"{stem}.{ext}" if ext else stem
    out = out.rstrip(". ") or fallback
    return out


def unique_filename(name: str, taken: set[str]) -> str:
    """Rende univoco un nome dentro una cartella, a parità di case."""
    candidate = safe_filename(name)
    if candidate.lower() not in taken:
        taken.add(candidate.lower())
        return candidate
    stem, dot, ext = candidate.rpartition(".")
    if not dot:
        stem, ext = candidate, ""
    i = 1
    while True:
        suffix = f"-{i}"
        trimmed = stem[: max(1, MAX_COMPONENT - len(ext) - len(suffix) - 1)]
        candidate = f"{trimmed}{suffix}.{ext}" if ext else f"{trimmed}{suffix}"
        if candidate.lower() not in taken:
            taken.add(candidate.lower())
            return candidate
        i += 1
