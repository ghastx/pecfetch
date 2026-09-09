"""Il tempo, reso in un modo solo.

Un record che porta la data certificata con l'offset del gestore e la data di
ricezione normalizzata a UTC non è sbagliato: è illeggibile. Chi lo consuma
confronta orari che appartengono a fusi diversi e prima o poi ne trae la
conclusione sbagliata.

Qui c'è la convenzione, in un posto solo: **ogni data esposta esce come ISO 8601
con l'offset del fuso configurato** (`[general].timezone`, per default
`Europe/Rome`), con precisione al secondo. Nessuna data esce in UTC, nessuna
data esce senza offset. Vale per l'indice, per i metadati, per l'archivio, per
lo stato locale e per i timestamp del log.

Il fuso di lettura resta quello che il dato dichiara: una data certificata
`+02:00` e una INTERNALDATE `+00:00` indicano lo stesso istante e continuano a
essere lette così. Cambia solo come le si scrive.

Le stringhe ISO con lo stesso offset si ordinano anche alfabeticamente, ma non
nell'ora del ritorno all'ora solare, quando `02:30+02:00` precede `02:30+01:00`
pur ordinandosi dopo. Dove l'ordine conta si usa `piu_recente`, che confronta
istanti; il confronto fra stringhe resta solo dove sbagliare di un'ora due volte
l'anno non cambia nulla.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: il fuso dello studio, non quello della macchina
DEFAULT_TZ = "Europe/Rome"


def zona(nome: str | None) -> ZoneInfo:
    """Il fuso, o un errore esplicito.

    Un refuso in configurazione non deve diventare un silenzioso ripiego su UTC:
    sarebbe esattamente la mescolanza che questo modulo esiste per togliere.
    """
    try:
        return ZoneInfo(str(nome or DEFAULT_TZ))
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"fuso orario sconosciuto: {nome!r}") from exc


def adesso(tz: ZoneInfo) -> datetime:
    """Ora corrente nel fuso dichiarato."""
    return datetime.now(tz)


def oggi(tz: ZoneInfo) -> date:
    """Giorno corrente nel fuso dichiarato, non in quello della macchina."""
    return datetime.now(tz).date()


def reso(value: datetime | None, tz: ZoneInfo) -> str | None:
    """Un istante nella forma del contratto, o `None` se non è un istante.

    Un datetime senza fuso non arriva mai dai parser di pecfetch, che lo
    dichiarano sempre; se arriva lo stesso lo si legge come UTC, che è la lettura
    prudente per una data di provenienza ignota.
    """
    if value is None:
        return None
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz).isoformat(timespec="seconds")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def ora(tz: ZoneInfo) -> str:
    """`adesso` già reso: è la forma che finisce quasi sempre su disco."""
    return adesso(tz).isoformat(timespec="seconds")


def letto(value: object) -> datetime | None:
    """Una stringa ISO -> datetime, o `None`. Regge anche le righe vecchie.

    Serve a confrontare istanti scritti prima di questa convenzione, quando la
    stessa colonna poteva portare offset diversi.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def piu_recente(a: str | None, b: str | None) -> str | None:
    """Il più recente fra due ISO, confrontando istanti e non stringhe."""
    if not a:
        return b
    if not b:
        return a
    da, db = letto(a), letto(b)
    if da is None or db is None:
        # una delle due non è una data: meglio l'ordine alfabetico che un errore
        return a if a > b else b
    return a if da > db else b


__all__ = ["DEFAULT_TZ", "zona", "adesso", "oggi", "ora", "reso", "letto",
           "piu_recente"]
