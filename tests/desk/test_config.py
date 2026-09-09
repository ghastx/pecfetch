"""I file versionati nel repository devono caricarsi davvero.

Come per pecfetch, l'esempio contiene percorsi assoluti di produzione: vanno
riscritti prima di caricarlo, altrimenti il test tocca il filesystem vero della
macchina su cui gira.
"""

from __future__ import annotations

from pathlib import Path

from pecdesk.config import load_config
from pecdesk.directives import load_directives

RADICE = Path(__file__).resolve().parents[2]


def test_esempio_versionato_e_valido(tmp_path, monkeypatch):
    esempio = RADICE / "config" / "pecdesk.example.toml"
    testo = esempio.read_text(encoding="utf-8")
    for produzione, finto in (("/etc/pecdesk", f"{tmp_path}/etc"),
                              ("/srv/pec/dati", f"{tmp_path}/dati"),
                              ("/var/lib/pecfetch", f"{tmp_path}/pecfetch"),
                              ("/var/lib/pecdesk", f"{tmp_path}/stato"),
                              ("/var/log/pecdesk", f"{tmp_path}/log")):
        testo = testo.replace(produzione, finto)
    copia = tmp_path / "pecdesk.toml"
    copia.write_text(testo, encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-finta")
    monkeypatch.setenv("PECDESK_SMTP_PASSWORD", "finta")

    cfg = load_config(copia, warn=lambda m: None)

    assert cfg.model and cfg.digest.to
    assert cfg.api_key_source.startswith("env:")
    # l'isolatezza si asserisce: un percorso assoluto aggiunto domani
    # all'esempio deve far fallire questo test
    for nome, percorso in (("output_root", cfg.output_root),
                           ("archive_path", cfg.archive_path),
                           ("state_dir", cfg.state_dir),
                           ("worked_dir", cfg.worked_dir),
                           ("rules_path", cfg.rules_path),
                           ("instructions_path", cfg.instructions_path),
                           ("log_file", cfg.log_file),
                           ("digest.copy_dir", cfg.digest.copy_dir)):
        assert percorso is not None, nome
        assert percorso.is_relative_to(tmp_path), f"{nome} esce da tmp_path: {percorso}"


def test_esempio_non_contiene_segreti(tmp_path):
    """Chiave API e password SMTP stanno fuori dal repository, sempre."""
    testo = (RADICE / "config" / "pecdesk.example.toml").read_text(encoding="utf-8")
    assert "sk-ant-" not in testo
    assert "api_key_env" in testo or "api_key_file" in testo
    assert "smtp_password_env" in testo or "smtp_password_file" in testo


def test_direttive_versionate_sono_valide():
    """Le direttive del repository sono il punto di partenza di ogni studio:
    se non caricano, non carica niente."""
    direttive = load_directives(RADICE / "direttive" / "regole.toml",
                                RADICE / "direttive" / "istruzioni.md")
    assert direttive.rules
    assert "titolare" in direttive.recipients
    assert direttive.instructions.strip()
    assert not direttive.warnings
    # ogni destinatario nominato da una regola deve esistere fra gli alias
    for regola in direttive.rules:
        if regola.recipient:
            assert ("@" in regola.recipient
                    or regola.recipient.lower() in direttive.recipients), regola.id
    # i tipi che vanno sempre al titolare devono stare nel vocabolario
    assert set(direttive.always_owner_types) <= set(direttive.doc_types)
