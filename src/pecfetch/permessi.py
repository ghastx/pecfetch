"""Permessi dei file prodotti: scelti, non ereditati dalla umask.

Senza questo modulo il risultato è arbitrario: `tempfile.mkdtemp` crea a `0700`
e quel modo sopravvive al `rename` che porta la cartella nella coda, mentre
tutto ciò che sta dentro nasce dalla umask del processo (`0775`/`0664` con la
umask di sistema). Nessuno ha scelto quella combinazione, e il consumatore a
valle non riesce nemmeno ad aprire la cartella.

Il modello, dichiarato e configurabile in `[permissions]`:

* cartelle `0750`, file `0640` — l'archivio PEC di uno studio non è leggibile da
  chiunque abbia un account sulla macchina;
* `coda/` ed `esiti/` a `2770` — il consumatore gira con un'altra identità,
  appartiene al gruppo, e deve poter **spostare** le cartelle lavorate fuori
  dalla coda e scrivere i propri esiti. Spostare una cartella richiede il
  permesso di scrittura sulla directory che la contiene, non sulla cartella;
* il bit setgid fa sì che ciò che il consumatore crea resti nel gruppo giusto
  senza dipendere dal suo gruppo primario.

Chi espone la radice in sola lettura dalla rete lo fa con un servizio che gira
con un utente del gruppo: si concede l'accesso a un servizio, non a tutti.
"""

from __future__ import annotations

import grp
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: i modi di default, quelli documentati nel README
DIR_MODE = 0o750
FILE_MODE = 0o640
SHARED_DIR_MODE = 0o2770


@dataclass(frozen=True)
class Permessi:
    """Il modello effettivo, uno per esecuzione."""

    dir_mode: int = DIR_MODE
    file_mode: int = FILE_MODE
    #: `coda/` ed `esiti/`: il consumatore ci scrive
    shared_dir_mode: int = SHARED_DIR_MODE
    #: gruppo da assegnare a ciò che si produce; vuoto = quello del processo
    group: str = ""

    @property
    def gid(self) -> int:
        """Il gid del gruppo richiesto, o -1 per «lascia quello che c'è»."""
        return _gid(self.group)


def modo(value: object, default: int) -> int:
    """Legge un modo da configurazione: `"0750"`, `"2770"`, oppure un intero.

    In TOML un intero che comincia per zero non è ottale, è un errore di
    sintassi: si dichiara come stringa e la si legge in base 8. Un intero
    scritto senza virgolette viene comunque interpretato in base 8, perché
    `755` in un file di permessi non ha mai voluto dire settecentocinquantacinque.
    """
    if value is None or value == "":
        return default
    try:
        parsed = int(str(value).strip(), 8)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"modo dei permessi non valido: {value!r}") from exc
    if not 0 <= parsed <= 0o7777:
        raise ValueError(f"modo dei permessi fuori intervallo: {value!r}")
    return parsed


def umask_da(perms: Permessi) -> int:
    """La umask che non toglie niente a questo modello.

    Rete di sicurezza per ciò che dovesse sfuggire ai chmod espliciti: con
    `0750`/`0640` vale `0o027`.
    """
    return 0o777 & ~(perms.dir_mode | perms.file_mode) & 0o777


def crea_dir(path: Path | str, mode: int, perms: Permessi | None = None) -> Path:
    """Crea una directory con il modo richiesto, qualunque sia la umask."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, mode)
    if perms is not None:
        _chgrp(path, perms.gid)
    return path


def applica_file(path: Path | str, perms: Permessi) -> None:
    """Porta un singolo file al modo dichiarato."""
    path = Path(path)
    _chmod(path, perms.file_mode)
    _chgrp(path, perms.gid)


def applica_albero(root: Path | str, perms: Permessi,
                   root_mode: int | None = None) -> None:
    """Applica il modello a un albero intero, radice compresa.

    Si chiama sul montaggio in `.tmp-pecfetch` **prima** del rename: la cartella
    compare nella coda già con i permessi giusti, e l'atomicità resta intatta.
    """
    root = Path(root)
    _chmod(root, root_mode if root_mode is not None else perms.dir_mode)
    _chgrp(root, perms.gid)
    for base, dirs, files in os.walk(root):
        for name in dirs:
            target = Path(base) / name
            _chmod(target, perms.dir_mode)
            _chgrp(target, perms.gid)
        for name in files:
            target = Path(base) / name
            _chmod(target, perms.file_mode)
            _chgrp(target, perms.gid)


def apri_append(path: Path | str, perms: Permessi, rileggibile: bool = False) -> int:
    """`os.open` in append con il modo dichiarato, anche su un file già esistente.

    La umask maschera il modo passato a `open`, e un file creato prima di questa
    convenzione conserverebbe il suo: il `fchmod` rende l'operazione idempotente.
    Con `rileggibile` il descrittore serve anche a rileggere (per controllare se
    l'ultima riga è rimasta a metà), quindi si apre in lettura e scrittura.
    """
    flags = (os.O_RDWR if rileggibile else os.O_WRONLY) | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, perms.file_mode)
    try:
        os.fchmod(fd, perms.file_mode)
        if perms.gid >= 0:
            os.fchown(fd, -1, perms.gid)
    except OSError as exc:
        _lamenta(path, exc)
    return fd


def applica_sqlite(path: Path | str, perms: Permessi) -> None:
    """Il database e i suoi sidecar WAL al modo dichiarato.

    `-wal` e `-shm` li crea la libreria SQLite con il modo del database, ma
    quello lo ha deciso la umask: senza questo passaggio il consumatore, che
    apre l'archivio in `mode=ro`, non riesce a leggere il WAL e quindi nemmeno
    il database.
    """
    path = Path(path)
    for target in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if target.exists():
            applica_file(target, perms)


def descrizione(perms: Permessi) -> str:
    """Come si stampa in `pecfetch check`."""
    gruppo = perms.group or "(quello del processo)"
    return (f"cartelle {perms.dir_mode & 0o7777:04o}, file {perms.file_mode:04o}, "
            f"coda/ ed esiti/ {perms.shared_dir_mode:04o}, gruppo {gruppo}")


def divergenze(root: Path | str, perms: Permessi, limite: int = 20) -> list[str]:
    """Elenca i percorsi il cui modo non corrisponde al modello. Non corregge."""
    root = Path(root)
    fuori: list[str] = []
    for base, dirs, files in os.walk(root):
        for name in list(dirs) + list(files):
            target = Path(base) / name
            atteso = perms.dir_mode if target.is_dir() else perms.file_mode
            try:
                attuale = target.stat().st_mode & 0o7777
            except OSError:
                continue
            if attuale != atteso:
                fuori.append(f"{target}: {attuale:04o} invece di {atteso:04o}")
                if len(fuori) >= limite:
                    return fuori
    return fuori


# -- interno ---------------------------------------------------------------

def _gid(group: str) -> int:
    if not group:
        return -1
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        log.warning("gruppo '%s' inesistente: i file restano nel gruppo del processo",
                    group)
        return -1


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        _lamenta(path, exc)


def _chgrp(path: Path, gid: int) -> None:
    if gid < 0:
        return
    try:
        os.chown(path, -1, gid)
    except OSError as exc:
        _lamenta(path, exc)


#: un permesso che non si riesce a impostare va detto una volta, non a ogni file:
#: su una coda di cinquecento messaggi il log diventerebbe illeggibile
_gia_detto: set[str] = set()


def _lamenta(path: Path | str, exc: OSError) -> None:
    chiave = str(getattr(exc, "errno", "")) or "?"
    if chiave in _gia_detto:
        return
    _gia_detto.add(chiave)
    log.warning("permessi non applicati su %s: %s", path, exc)


__all__ = ["Permessi", "DIR_MODE", "FILE_MODE", "SHARED_DIR_MODE", "modo",
           "umask_da", "crea_dir", "applica_file", "applica_albero",
           "apri_append", "applica_sqlite", "descrizione", "divergenze"]
