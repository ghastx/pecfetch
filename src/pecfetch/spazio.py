"""Guardia sullo spazio disponibile.

I limiti sugli allegati e sugli archivi proteggono dal singolo allegato
patologico; non proteggono dall'accumulo di una coda che nessuno svuota. E un
filesystem pieno non blocca solo la scrittura del messaggio: blocca anche le
scritture di stato, cioè la parte che non può fallire, perché è l'unica verità
su cosa è già stato scaricato.

Quindi si guarda prima di scrivere, non dopo: sotto la soglia si rifiuta pulito,
si annota, e non si avanza il cursore. Alla prossima esecuzione, liberato lo
spazio, si riprende da dove ci si era fermati.
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: sotto questa soglia non si comincia nemmeno un messaggio
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024   # 1 GiB


def liberi(path: Path | str) -> int:
    """Byte liberi sul filesystem che ospita `path`, o -1 se non si sa.

    Il percorso può non esistere ancora (prima esecuzione): si risale al primo
    antenato che esiste, che sta comunque sullo stesso filesystem.
    """
    candidate = Path(path).resolve()
    for target in [candidate, *candidate.parents]:
        if target.exists():
            try:
                return shutil.disk_usage(target).free
            except OSError:
                return -1
    return -1


def sufficiente(paths, soglia: int, margine: int = 0) -> tuple[bool, str]:
    """(va bene, motivo). `margine` è quanto sta per essere scritto.

    Si controllano tutti i percorsi passati — la radice dei dati e lo stato —
    perché possono stare su filesystem diversi e basta che uno sia pieno.
    """
    if soglia <= 0 and margine <= 0:
        return True, ""
    visti: set[int] = set()
    for path in paths:
        path = Path(path)
        try:
            chiave = _filesystem(path)
        except OSError:
            chiave = None
        if chiave is not None:
            if chiave in visti:
                continue
            visti.add(chiave)
        disponibili = liberi(path)
        if disponibili < 0:
            # non si riesce a interrogare il filesystem: non è un buon motivo
            # per fermare un'acquisizione, e la scrittura fallirà con il suo
            # errore vero se lo spazio manca davvero
            continue
        if disponibili - margine < soglia:
            return False, (
                f"{path}: {leggibile(disponibili)} liberi"
                + (f", {leggibile(margine)} da scrivere" if margine else "")
                + f", soglia {leggibile(soglia)}"
            )
    return True, ""


def leggibile(n: int) -> str:
    """Byte in una forma che si legge in un log."""
    if n < 0:
        return "?"
    valore = float(n)
    for unita in ("B", "KiB", "MiB", "GiB", "TiB"):
        if valore < 1024 or unita == "TiB":
            return f"{valore:.0f} {unita}" if unita == "B" else f"{valore:.1f} {unita}"
        valore /= 1024
    return f"{valore:.1f} TiB"


def _filesystem(path: Path) -> int | None:
    for target in [path.resolve(), *path.resolve().parents]:
        if target.exists():
            return target.stat().st_dev
    return None


__all__ = ["DEFAULT_MIN_FREE_BYTES", "liberi", "sufficiente", "leggibile"]
