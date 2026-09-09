"""La guardia sullo spazio: rifiuto pulito, non un errore a metà scrittura."""

from collections import namedtuple

import factories as f
from conftest import FakeMailbox
from pecfetch import spazio
from pecfetch.pipeline import MODE_RUN

_Uso = namedtuple("_Uso", "total used free")


def _disco_pieno(monkeypatch, liberi: int) -> None:
    monkeypatch.setattr(spazio.shutil, "disk_usage",
                        lambda path: _Uso(100, 100 - liberi, liberi))


def test_leggibile():
    assert spazio.leggibile(0) == "0 B"
    assert spazio.leggibile(1536) == "1.5 KiB"
    assert spazio.leggibile(-1) == "?"


def test_sufficiente_tiene_conto_di_quanto_sta_per_scrivere(tmp_path, monkeypatch):
    _disco_pieno(monkeypatch, 10_000)
    assert spazio.sufficiente([tmp_path], 1_000)[0] is True
    ok, motivo = spazio.sufficiente([tmp_path], 1_000, margine=9_500)
    assert ok is False
    assert "da scrivere" in motivo


def test_soglia_zero_non_controlla_niente(tmp_path, monkeypatch):
    _disco_pieno(monkeypatch, 0)
    assert spazio.sufficiente([tmp_path], 0)[0] is True


def test_sotto_soglia_non_si_scrive_e_il_cursore_non_avanza(
        cfg, stack, make_pipeline, monkeypatch):
    """Nel dubbio si riscarica: i messaggi restano sulla casella."""
    from pecfetch.config import replace

    state = stack[0]
    box = FakeMailbox()
    box.add(f.busta_trasporto(), None)
    pipeline = make_pipeline({"rossi": box})
    pipeline.cfg = replace(cfg, min_free_bytes=10 * 1024 * 1024)
    _disco_pieno(monkeypatch, 1024)

    summary = pipeline.run(MODE_RUN)

    assert summary.written == 0
    assert summary.accounts_err == 1
    assert "spazio insufficiente" in summary.errors[0][1]
    assert not (cfg.output_root / "coda").exists() or \
        list((cfg.output_root / "coda").iterdir()) == []
    assert state.get_cursor("rossi", "INBOX").last_uid == 0


def test_ci_si_ferma_prima_del_messaggio_che_non_ci_sta(
        cfg, stack, make_pipeline, monkeypatch):
    """Lo spazio basta per stare sopra soglia, non per scrivere il messaggio."""
    from pecfetch.config import replace

    state = stack[0]
    soglia = 10 * 1024 * 1024
    box = FakeMailbox()
    box.add(f.busta_trasporto(), None)
    box.add(f.busta_trasporto(subject="POSTA CERTIFICATA: secondo"), None)
    pipeline = make_pipeline({"rossi": box})
    pipeline.cfg = replace(cfg, min_free_bytes=soglia)
    _disco_pieno(monkeypatch, soglia + 100)     # sopra soglia, ma di niente

    summary = pipeline.run(MODE_RUN)

    assert summary.written == 0
    assert summary.remaining == 2          # restano tutti sulla casella
    assert summary.accounts_err == 1
    assert state.get_cursor("rossi", "INBOX").last_uid == 0


def test_sopra_soglia_tutto_come_prima(cfg, stack, make_pipeline, monkeypatch):
    box = FakeMailbox()
    box.add(f.busta_trasporto(), None)
    _disco_pieno(monkeypatch, 10 * 1024 * 1024 * 1024)
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)
    assert summary.written == 1
    assert summary.accounts_err == 0
