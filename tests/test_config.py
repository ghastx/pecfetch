"""Configurazione fuori dal codice, credenziali fuori dal repository."""

import pytest

from pecfetch.config import ConfigError, load_config, redacted

BASE = """
[general]
output_root = "{root}/condivisa"
state_dir = "{root}/stato"
{extra}

[fetch]
initial_lookback_days = 3

[[accounts]]
id = "rossi"
label = "Rossi S.r.l."
client_id = "ROSSI"
address = "rossi@pec.it"
host = "imaps.pec.aruba.it"
{cred}
"""


def _scrivi(tmp_path, cred='password_env = "PECFETCH_TEST_PW"', extra=""):
    path = tmp_path / "pecfetch.toml"
    path.write_text(BASE.format(root=tmp_path, cred=cred, extra=extra), encoding="utf-8")
    return path


def test_caricamento_minimo(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "segretissima")
    cfg = load_config(_scrivi(tmp_path))
    assert len(cfg.accounts) == 1
    account = cfg.accounts[0]
    assert account.id == "rossi" and account.port == 993 and account.ssl is True
    assert account.username == "rossi@pec.it"
    assert account.password == "segretissima"
    assert cfg.initial_lookback_days == 3
    # l'archivio sta di default nello stato locale, non sulla condivisa
    assert cfg.archive_path.parent == cfg.state_dir


def test_password_mancante_e_un_errore(tmp_path, monkeypatch):
    monkeypatch.delenv("PECFETCH_TEST_PW", raising=False)
    with pytest.raises(ConfigError, match="PECFETCH_TEST_PW"):
        load_config(_scrivi(tmp_path))


def test_password_da_file_dei_segreti(tmp_path):
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[passwords]\nrossi = "dal-file"\n', encoding="utf-8")
    path = _scrivi(tmp_path, cred="", extra='secrets_file = "secrets.toml"')
    cfg = load_config(path)
    assert cfg.accounts[0].password == "dal-file"
    assert cfg.accounts[0].password_source == "secrets"


def test_password_in_chiaro_avvisa(tmp_path):
    avvisi = []
    load_config(_scrivi(tmp_path, cred='password = "in-chiaro"'), warn=avvisi.append)
    assert any("chiaro" in a for a in avvisi)


def test_la_password_non_compare_mai_nella_vista_loggabile(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "segretissima")
    cfg = load_config(_scrivi(tmp_path))
    dump = str(redacted(cfg))
    assert "segretissima" not in dump
    assert "env:PECFETCH_TEST_PW" in dump


def test_id_duplicato(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    path = _scrivi(tmp_path)
    path.write_text(path.read_text() + '\n[[accounts]]\nid = "rossi"\n'
                    'address = "altro@pec.it"\nhost = "h"\n'
                    'password_env = "PECFETCH_TEST_PW"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicato"):
        load_config(path)


def test_host_mancante(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    path = tmp_path / "c.toml"
    path.write_text(f'[general]\noutput_root = "{tmp_path}/o"\n\n'
                    '[[accounts]]\nid = "x"\naddress = "x@pec.it"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="host"):
        load_config(path)


def test_file_inesistente(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "non-esiste.toml")


def test_casella_disabilitata_non_richiede_password(tmp_path, monkeypatch):
    monkeypatch.delenv("PECFETCH_TEST_PW", raising=False)
    cfg = load_config(_scrivi(tmp_path, cred="enabled = false"))
    assert cfg.accounts[0].enabled is False


def test_accounts_file_separato(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    (tmp_path / "caselle.toml").write_text(
        '[[accounts]]\nid = "bianchi"\naddress = "b@pec.it"\nhost = "h"\n'
        '\n[passwords]\nbianchi = "dal-file-caselle"\n', encoding="utf-8")
    path = tmp_path / "pecfetch.toml"
    path.write_text(f'[general]\noutput_root = "{tmp_path}/o"\n'
                    'accounts_file = "caselle.toml"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.accounts[0].id == "bianchi"
    assert cfg.accounts[0].password == "dal-file-caselle"


def test_esempio_versionato_e_valido(tmp_path, monkeypatch):
    """Il file di esempio nel repository deve caricarsi davvero."""
    from pathlib import Path

    esempio = Path(__file__).resolve().parents[1] / "config" / "pecfetch.example.toml"
    testo = esempio.read_text(encoding="utf-8")
    copia = tmp_path / "pecfetch.toml"
    copia.write_text(testo, encoding="utf-8")
    (tmp_path / "caselle.example.toml").write_text(
        (esempio.parent / "caselle.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8")
    monkeypatch.setenv("PECFETCH_PW_ROSSI", "x")
    monkeypatch.setenv("PECFETCH_PW_BIANCHI", "y")
    cfg = load_config(copia, warn=lambda m: None)
    assert cfg.accounts
