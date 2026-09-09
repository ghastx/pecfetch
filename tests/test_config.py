"""Configurazione fuori dal codice, credenziali fuori dal repository."""

import os
from pathlib import Path

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
    """Il file di esempio nel repository deve caricarsi davvero.

    L'esempio contiene percorsi assoluti di produzione (`/etc/pecfetch`,
    `/srv/pec/dati`, ...). Vanno riscritti prima di caricarlo: altrimenti su una
    macchina dove pecfetch è installato questo test leggerebbe i segreti veri, e
    fallirebbe appena quel file avesse i permessi che `INSTALL.md` consiglia.
    """
    esempio = Path(__file__).resolve().parents[1] / "config" / "pecfetch.example.toml"
    testo = esempio.read_text(encoding="utf-8")
    for produzione, finto in (("/etc/pecfetch", f"{tmp_path}/etc"),
                              ("/srv/pec/dati", f"{tmp_path}/dati"),
                              ("/var/lib/pecfetch", f"{tmp_path}/stato"),
                              ("/var/log/pecfetch", f"{tmp_path}/log")):
        testo = testo.replace(produzione, finto)

    copia = tmp_path / "pecfetch.toml"
    copia.write_text(testo, encoding="utf-8")
    (tmp_path / "caselle.example.toml").write_text(
        (esempio.parent / "caselle.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8")
    monkeypatch.setenv("PECFETCH_PW_ROSSI", "x")
    monkeypatch.setenv("PECFETCH_PW_BIANCHI", "y")

    cfg = load_config(copia, warn=lambda m: None)

    assert cfg.accounts
    # l'isolatezza va asserita, non sperata: un percorso assoluto aggiunto
    # domani all'esempio deve far fallire questo test, non tornare di nascosto
    # a puntare al filesystem vero
    for nome, percorso in (("output_root", cfg.output_root),
                           ("state_dir", cfg.state_dir),
                           ("archive_path", cfg.archive_path),
                           ("log_file", cfg.log_file)):
        assert percorso is not None, nome
        assert percorso.is_relative_to(tmp_path), f"{nome} esce da tmp_path: {percorso}"


# ---------------------------------------------------------------------------
# il file dei segreti: assente è tollerato, illeggibile no
# ---------------------------------------------------------------------------

def test_segreti_assenti_restano_un_avviso(tmp_path, monkeypatch):
    """Le password possono venire da password_env: un file che non c'è non è
    un errore, ed è la distinzione che i due test successivi difendono."""
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    avvisi = []
    cfg = load_config(
        _scrivi(tmp_path, extra=f'secrets_file = "{tmp_path}/manca.toml"'),
        warn=avvisi.append,
    )
    assert cfg.accounts[0].password == "x"
    assert any("non trovato" in a for a in avvisi)


def test_segreti_che_sono_una_directory_danno_errore_di_configurazione(tmp_path):
    """Prima si avvisava «non trovato» — che è falso — e si moriva molto più a
    valle con «nessuna password», che non c'entra niente."""
    (tmp_path / "secrets.toml").mkdir()
    with pytest.raises(ConfigError, match="segreti non leggibile"):
        load_config(_scrivi(tmp_path, cred='password_ref = "rossi"',
                            extra=f'secrets_file = "{tmp_path}/secrets.toml"'))


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root ignora i permessi: il test non proverebbe niente")
def test_segreti_senza_permessi_danno_errore_di_configurazione(tmp_path):
    """Il `chmod 600 root:root` che sta a un tasto da quello giusto in
    INSTALL.md: prima usciva come traccia di stack, non come diagnosi."""
    segreti = tmp_path / "secrets.toml"
    segreti.write_text('[passwords]\nrossi = "x"\n', encoding="utf-8")
    segreti.chmod(0o000)
    with pytest.raises(ConfigError, match="non leggibile"):
        load_config(_scrivi(tmp_path, cred='password_ref = "rossi"',
                            extra=f'secrets_file = "{segreti}"'))


def test_accounts_file_illeggibile_da_errore_di_configurazione(tmp_path, monkeypatch):
    """La stessa lacuna valeva per l'elenco delle caselle."""
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    (tmp_path / "caselle.toml").mkdir()
    with pytest.raises(ConfigError):
        load_config(_scrivi(tmp_path, extra='accounts_file = "caselle.toml"'))


def test_indirizzo_della_casella_normalizzato(tmp_path, monkeypatch):
    """Configurata in maiuscolo o in minuscolo è la stessa casella."""
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    path = tmp_path / "pecfetch.toml"
    path.write_text(
        BASE.format(root=tmp_path, cred='password_env = "PECFETCH_TEST_PW"', extra="")
        .replace("rossi@pec.it", "ROSSI@PEC.IT"),
        encoding="utf-8")
    cfg = load_config(path)
    assert cfg.accounts[0].address == "rossi@pec.it"
    # lo username no: è quello che si manda al gestore, non un dato del contratto
    assert cfg.accounts[0].username == "ROSSI@PEC.IT"


def test_due_caselle_sullo_stesso_indirizzo_sono_un_errore(tmp_path, monkeypatch):
    """Sarebbero due cursori che si ignorano sulla stessa casella."""
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    path = tmp_path / "pecfetch.toml"
    path.write_text(
        BASE.format(root=tmp_path, cred='password_env = "PECFETCH_TEST_PW"', extra="")
        + '\n[[accounts]]\nid = "rossi2"\naddress = "ROSSI@PEC.IT"\n'
          'host = "imaps.pec.aruba.it"\npassword_env = "PECFETCH_TEST_PW"\n',
        encoding="utf-8")
    with pytest.raises(ConfigError, match="già dichiarato"):
        load_config(path)


def test_casella_cercata_senza_badare_alle_maiuscole(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    cfg = load_config(_scrivi(tmp_path))
    assert cfg.account_by_id("ROSSI") is cfg.accounts[0]
    assert cfg.account_by_id("bianchi") is None


def test_permessi_e_spazio_hanno_un_default_dichiarato(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    cfg = load_config(_scrivi(tmp_path))
    assert (cfg.permissions.dir_mode, cfg.permissions.file_mode) == (0o750, 0o640)
    assert cfg.permissions.shared_dir_mode == 0o2770
    assert cfg.min_free_bytes == 1024 * 1024 * 1024
    assert "0750" in redacted(cfg)["permissions"]


def test_permessi_configurabili(tmp_path, monkeypatch):
    monkeypatch.setenv("PECFETCH_TEST_PW", "x")
    path = _scrivi(tmp_path)
    path.write_text(path.read_text(encoding="utf-8")
                    + '\n[permissions]\ndir_mode = "0755"\nfile_mode = "0644"\n'
                      'shared_dir_mode = "2775"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.permissions.dir_mode == 0o755
    assert cfg.permissions.shared_dir_mode == 0o2775
