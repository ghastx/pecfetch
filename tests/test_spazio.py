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


def test_il_rifiuto_si_vede_in_status(cfg, stack, make_pipeline, monkeypatch):
    """Chi guarda `pecfetch status` deve trovare il motivo, non l'esito di ieri."""
    from pecfetch.config import replace

    state = stack[0]
    box = FakeMailbox()
    box.add(f.busta_trasporto(), None)
    pipeline = make_pipeline({"rossi": box})
    pipeline.cfg = replace(cfg, min_free_bytes=10 * 1024 * 1024)
    _disco_pieno(monkeypatch, 1024)

    pipeline.run(MODE_RUN)

    riga = state.mailbox_rows()[0]
    assert "spazio insufficiente" in (riga["last_error"] or "")
    assert riga["last_run_at"]
    assert riga["last_ok_at"] is None


def test_in_prova_a_vuoto_non_si_annota_niente(cfg, stack, make_pipeline, monkeypatch):
    """La prova a vuoto avvisa nel log, ma non tocca lo stato."""
    from pecfetch.config import replace

    state = stack[0]
    box = FakeMailbox()
    box.add(f.busta_trasporto(), None)
    pipeline = make_pipeline({"rossi": box})
    pipeline.cfg = replace(cfg, min_free_bytes=10 * 1024 * 1024)
    _disco_pieno(monkeypatch, 1024)

    summary = pipeline.run(MODE_RUN, dry_run=True)

    assert summary.accounts_err == 0        # non si ferma: dice solo che si fermerebbe
    assert [r["last_error"] for r in state.mailbox_rows()] in ([], [None])


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
    from pecfetch.config import replace

    box = FakeMailbox()
    box.add(f.busta_trasporto(), None)
    pipeline = make_pipeline({"rossi": box})
    pipeline.cfg = replace(cfg, min_free_bytes=10 * 1024 * 1024)
    _disco_pieno(monkeypatch, 10 * 1024 * 1024 * 1024)

    summary = pipeline.run(MODE_RUN)

    assert summary.written == 1
    assert summary.accounts_err == 0


def test_la_fixture_non_guarda_il_disco_vero(cfg):
    """Canarino: la guardia sta spenta ovunque tranne che qui dentro.

    Con la soglia di default l'esito di mezza suite dipenderebbe da quanto
    spazio ha in quel momento la macchina di chi esegue i test — che è lo stesso
    difetto del dipendere da un server IMAP, o da tesseract installato. Se
    qualcuno toglie `min_free_bytes=0` dalla fixture, deve fallire questa riga,
    che dice perché, non venti test che parlano d'altro.
    """
    assert cfg.min_free_bytes == 0
