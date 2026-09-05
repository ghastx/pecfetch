"""Nessun run sovrapposto."""

import pytest

from pecfetch.lock import AlreadyRunning, RunLock


def test_secondo_lock_fallisce(tmp_path):
    primo = RunLock(tmp_path / "l.lock")
    primo.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            RunLock(tmp_path / "l.lock").acquire()
    finally:
        primo.release()


def test_dopo_il_rilascio_si_puo_riprendere(tmp_path):
    with RunLock(tmp_path / "l.lock"):
        pass
    with RunLock(tmp_path / "l.lock"):
        pass


def test_il_lock_riporta_il_pid(tmp_path):
    import os

    with RunLock(tmp_path / "l.lock"):
        assert (tmp_path / "l.lock").read_text().strip() == str(os.getpid())
