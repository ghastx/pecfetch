"""Lock di esecuzione: mai due run sovrapposti."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

from . import permessi as perm


class AlreadyRunning(Exception):
    """Un'altra esecuzione di pecfetch è già in corso."""


class RunLock:
    def __init__(self, path: str | Path, permissions: perm.Permessi | None = None):
        self.path = Path(path)
        self.permessi = permissions or perm.Permessi()
        perm.crea_dir(self.path.parent, self.permessi.dir_mode, self.permessi)
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, self.permessi.file_mode)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                holder = ""
                try:
                    holder = self.path.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                raise AlreadyRunning(
                    f"esecuzione già in corso{f' (pid {holder})' if holder else ''}"
                ) from exc
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


__all__ = ["RunLock", "AlreadyRunning"]
