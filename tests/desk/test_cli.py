"""La riga di comando, con una configurazione vera su disco."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from desk.fabbrica import ISTRUZIONI, REGOLE, make_attachment, write_message

from pecdesk.cli import EXIT_OK, EXIT_PARTIAL, main


def _setup(tmp_path) -> tuple[str, Path]:
    root = tmp_path / "dati"
    (root / "coda").mkdir(parents=True)
    (tmp_path / "direttive").mkdir()
    (tmp_path / "direttive" / "regole.toml").write_text(REGOLE, encoding="utf-8")
    (tmp_path / "direttive" / "istruzioni.md").write_text(ISTRUZIONI, encoding="utf-8")
    config = tmp_path / "pecdesk.toml"
    config.write_text(f"""
[general]
output_root = "{root}"
state_dir = "{tmp_path / 'stato'}"
archive_path = "{tmp_path / 'archivio.sqlite3'}"
min_free_bytes = 0

[direttive]
regole = "{tmp_path / 'direttive' / 'regole.toml'}"
istruzioni = "{tmp_path / 'direttive' / 'istruzioni.md'}"

[modello]
id = "claude-haiku-4-5"

[riepilogo]
destinatario = "titolare@studio.it"
invia = false
copia_in = "{tmp_path / 'riepiloghi'}"

[logging]
level = "WARNING"
""", encoding="utf-8")
    return str(config), root / "coda"


def test_prova_non_chiama_niente_e_non_tocca_niente(tmp_path, capsys):
    config, queue = _setup(tmp_path)
    write_message(queue, "m1", sender="sconosciuto@pec.it",
                  subject="Avviso di accertamento",
                  attachments=[make_attachment("atto.pdf", method="pdf_ocr",
                                               text="Il termine per il ricorso "
                                                    "scade entro il 03/10/2026. "
                                                    + "x" * 4000)])
    prima = sorted(p.name for p in queue.iterdir())

    assert main(["-c", config, "run", "--prova"]) == EXIT_OK
    uscita = capsys.readouterr().out

    assert "materiale non fidato che verrebbe inviato" in uscita
    assert "token" in uscita
    assert "nessuna chiamata al modello" in uscita
    assert "taglio:" in uscita                      # i tagli sono dichiarati
    # niente è stato scritto, niente è stato spostato
    assert sorted(p.name for p in queue.iterdir()) == prima
    assert not (tmp_path / "stato" / "stato.sqlite3").exists()
    assert not list((tmp_path / "dati" / "esiti").glob("*.jsonl"))


def test_prova_mostra_quando_una_regola_evita_la_chiamata(tmp_path, capsys):
    config, queue = _setup(tmp_path)
    write_message(queue, "m1", msg_type="errore_consegna")
    main(["-c", config, "run", "--prova"])
    assert "nessuna chiamata: la regola decide da sola" in capsys.readouterr().out


def test_check_racconta_la_configurazione_senza_segreti(tmp_path, capsys,
                                                        monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-segretissima")
    config, queue = _setup(tmp_path)
    write_message(queue, "m1")

    main(["-c", config, "check"])
    uscita = capsys.readouterr().out

    assert "sk-ant-segretissima" not in uscita
    assert "direttive: versione test-1" in uscita
    assert "sempre al titolare" in uscita
    assert "sola lettura" in uscita or "non disponibile" in uscita


def test_run_senza_chiave_non_inventa_classificazioni(tmp_path, capsys):
    config, queue = _setup(tmp_path)
    write_message(queue, "m1", sender="sconosciuto@pec.it")

    codice = main(["-c", config, "run"])
    uscita = capsys.readouterr().out

    assert codice == EXIT_PARTIAL
    assert "chiave API" in uscita
    assert len(list(queue.iterdir())) == 1            # resta in coda
    # ...ma il riepilogo esce lo stesso e lo dice
    copia = (tmp_path / "riepiloghi").glob("*.txt")
    testo = next(copia).read_text(encoding="utf-8")
    assert "NON LAVORATI" in testo


def test_correggi_e_registro(tmp_path, capsys):
    config, queue = _setup(tmp_path)
    write_message(queue, "m1", msg_type="errore_consegna")
    main(["-c", config, "run", "--senza-riepilogo"])
    capsys.readouterr()

    assert main(["-c", config, "correggi", "m1", "--campo", "tipo_documento",
                 "--valore", "ricevuta_negativa", "--autore", "titolare"]) == EXIT_OK
    assert "correzione registrata" in capsys.readouterr().out

    assert main(["-c", config, "registro"]) == EXIT_OK
    uscita = capsys.readouterr().out
    assert "esiti: 1" in uscita and "correzioni: 1" in uscita
    assert "origine dell'instradamento" in uscita


def test_spiega_mostra_esito_e_correzione(tmp_path, capsys):
    config, queue = _setup(tmp_path)
    write_message(queue, "m1", msg_type="errore_consegna")
    main(["-c", config, "run", "--senza-riepilogo"])
    main(["-c", config, "correggi", "m1", "--campo", "instradamento",
          "--valore", "titolare"])
    capsys.readouterr()

    assert main(["-c", config, "spiega", "m1"]) == EXIT_OK
    uscita = capsys.readouterr().out
    assert '"id": "m1"' in uscita
    assert "correzione:" in uscita


def test_stato_e_riprova(tmp_path, capsys):
    config, queue = _setup(tmp_path)
    write_message(queue, "m1", sender="sconosciuto@pec.it")
    main(["-c", config, "run", "--senza-riepilogo"])
    capsys.readouterr()

    assert main(["-c", config, "stato"]) == EXIT_OK
    assert "per_stato" in capsys.readouterr().out
    assert main(["-c", config, "riprova", "m1"]) == EXIT_OK
    assert "sbloccato" in capsys.readouterr().out


def test_configurazione_mancante_e_un_errore_chiaro(tmp_path, capsys):
    assert main(["-c", str(tmp_path / "assente.toml"), "check"]) == 1
    assert "nessun file di configurazione trovato" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root ignora i permessi: il test non proverebbe niente")
def test_check_segnala_la_coda_non_scrivibile(tmp_path, capsys):
    """Uscire dalla coda è un `rename`, e spostare una cartella richiede il
    permesso sulla directory che la contiene. È il difetto dell'unit systemd che
    si è già pagato una volta: `check` deve trovarlo prima della notte, non
    scoprirlo a spostamento fallito."""
    config, queue = _setup(tmp_path)
    write_message(queue, "m1")
    modo = queue.stat().st_mode
    os.chmod(queue, 0o555)
    try:
        codice = main(["-c", config, "check"])
        uscita = capsys.readouterr().out
    finally:
        os.chmod(queue, modo)

    assert codice == EXIT_PARTIAL
    assert "MANCANTE o non scrivibile" in uscita
    assert "rename fuori dalla coda" in uscita
    assert "ReadWritePaths" in uscita


def test_check_dice_perche_la_coda_serve_in_scrittura(tmp_path, capsys, monkeypatch):
    """Come sopra, ma senza dipendere dai permessi veri: il test gira anche da
    root, dove `os.access` direbbe sempre di sì."""
    from pecdesk import cli

    config, queue = _setup(tmp_path)
    write_message(queue, "m1")
    vero = cli.os.access
    monkeypatch.setattr(cli.os, "access",
                        lambda p, m: False if Path(p) == queue else vero(p, m))

    codice = main(["-c", config, "check"])
    uscita = capsys.readouterr().out

    assert codice == EXIT_PARTIAL
    assert "rename fuori dalla coda" in uscita
    assert "ReadWritePaths" in uscita
