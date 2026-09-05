"""Riga di comando: codici di uscita e comandi che non richiedono IMAP."""

import factories as f
import pytest

from pecfetch.cli import EXIT_FATAL, EXIT_LOCKED, EXIT_OK, main
from pecfetch.lock import RunLock


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_PW_TEST", "segretissima")
    path = tmp_path / "pecfetch.toml"
    path.write_text(f"""
[general]
output_root = "{tmp_path}/condivisa"
state_dir = "{tmp_path}/stato"

[[accounts]]
id = "rossi"
label = "Rossi S.r.l."
address = "rossi@pec.it"
host = "imap.example"
password_env = "PECFETCH_PW_TEST"
""", encoding="utf-8")
    return path


def test_parse_di_un_eml_locale_senza_configurazione(tmp_path, capsys):
    eml = tmp_path / "busta.eml"
    eml.write_bytes(f.busta_trasporto())
    assert main(["parse", str(eml)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "posta_certificata" in out
    assert "ente@pec.comune.it" in out
    assert "Avviso di accertamento" in out


def test_parse_json(tmp_path, capsys):
    import json

    eml = tmp_path / "r.eml"
    eml.write_bytes(f.ricevuta("errore-consegna"))
    assert main(["parse", "--json", str(eml)]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["tipo"] == "errore_consegna" and data["classe"] == "negativa"


def test_configurazione_assente_e_fatale(tmp_path):
    assert main(["-c", str(tmp_path / "manca.toml"), "status"]) == EXIT_FATAL


def test_status_su_stato_vuoto(config_file, capsys):
    assert main(["-c", str(config_file), "status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "rossi" in out and "da inizializzare" in out
    # nessuna password nell'output
    assert "segretissima" not in out


def test_status_json(config_file, capsys):
    import json

    assert main(["-c", str(config_file), "status", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["configurazione"]["accounts"][0]["password"] == "<env:PECFETCH_PW_TEST>"


def test_run_con_lock_occupato(config_file, tmp_path):
    (tmp_path / "stato").mkdir(parents=True, exist_ok=True)
    lock = RunLock(tmp_path / "stato" / "pecfetch.lock")
    lock.acquire()
    try:
        assert main(["-c", str(config_file), "run"]) == EXIT_LOCKED
    finally:
        lock.release()


def test_casella_sconosciuta(config_file):
    assert main(["-c", str(config_file), "run", "-a", "inesistente"]) == EXIT_FATAL


def test_check_non_richiede_rete(config_file, capsys):
    codice = main(["-c", str(config_file), "check"])
    out = capsys.readouterr().out
    assert "strumenti di estrazione" in out
    assert codice in (EXIT_OK, 2)   # 2 se mancano tesseract/poppler sulla macchina


def test_search_su_archivio_vuoto(config_file, capsys):
    assert main(["-c", str(config_file), "search", "qualsiasi"]) == EXIT_OK
    assert "nessun risultato" in capsys.readouterr().out


def test_receipts_vuote(config_file, capsys):
    assert main(["-c", str(config_file), "receipts"]) == EXIT_OK
    assert "nessuna ricevuta" in capsys.readouterr().out


def test_backfill_con_data_invalida(config_file):
    assert main(["-c", str(config_file), "backfill", "--since", "ieri"]) == EXIT_FATAL
