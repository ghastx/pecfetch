"""I permessi dell'albero prodotto: scelti, non ereditati dalla umask."""

import os
import stat

import pytest

import factories as f
from pecfetch import permessi as perm
from pecfetch.config import ConfigError
from pecfetch.pec import parse_pec


@pytest.fixture(autouse=True)
def umask_ostile():
    """Ogni test gira con una umask che, da sola, darebbe il risultato sbagliato."""
    precedente = os.umask(0o000)
    yield
    os.umask(precedente)


def _modo(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_modo_si_legge_in_ottale():
    assert perm.modo("0750", 0) == 0o750
    assert perm.modo("2770", 0) == 0o2770
    assert perm.modo(755, 0) == 0o755        # senza virgolette vale lo stesso
    assert perm.modo(None, 0o640) == 0o640
    with pytest.raises(ValueError):
        perm.modo("rwxr-x---", 0)
    with pytest.raises(ValueError):
        perm.modo("77777", 0)


def test_umask_derivata_non_toglie_niente_al_modello():
    assert perm.umask_da(perm.Permessi()) == 0o027
    largo = perm.Permessi(dir_mode=0o755, file_mode=0o644)
    assert perm.umask_da(largo) == 0o022


def test_albero_del_messaggio_al_modello_dichiarato(cfg, stack, account):
    """Il difetto segnalato: cartella del messaggio 0700, allegati 0775."""
    _state, _archive, writer = stack
    inner = f.inner_message(
        attachments=[("atto.pdf", b"%PDF-1.4 finto", "pdf")])
    raw = f.busta_trasporto(postacert=inner)
    pm = parse_pec(raw)
    result = writer.write_message(pm, raw, account, "id1", 1, 1, "2026-09-05T10:00:00+02:00")

    cartella = cfg.output_root / result.content_dir
    assert _modo(cartella) == 0o750
    assert _modo(cartella / "allegati") == 0o750
    assert _modo(cartella / "busta.eml") == 0o640
    assert _modo(cartella / "messaggio.json") == 0o640
    for allegato in (cartella / "allegati").iterdir():
        assert _modo(allegato) == 0o640

    writer.append_index(result.index_record)
    riga = next((cfg.output_root / "indice").glob("*.jsonl"))
    assert _modo(riga) == 0o640


def test_coda_ed_esiti_sono_scrivibili_dal_gruppo(cfg, stack):
    """Il consumatore sposta cartelle: serve scrittura sulla directory."""
    _state, _archive, _writer = stack
    assert _modo(cfg.output_root / "coda") == 0o2770
    assert _modo(cfg.output_root / "esiti") == 0o2770
    # l'indice invece lui lo legge soltanto
    assert _modo(cfg.output_root / "indice") == 0o750
    assert _modo(cfg.output_root / "CONTRATTO.md") == 0o640


def test_modello_configurabile(cfg, stack, account):
    from pecfetch.extract import ExtractorSettings
    from pecfetch.output import OutputWriter

    aperto = perm.Permessi(dir_mode=0o755, file_mode=0o644,
                           shared_dir_mode=0o2775)
    radice = cfg.output_root.parent / "aperta"
    writer = OutputWriter(radice, ExtractorSettings(enabled=False),
                          permissions=aperto)
    assert _modo(radice / "coda") == 0o2775
    assert _modo(radice / "indice") == 0o755
    raw = f.busta_trasporto()
    result = writer.write_message(parse_pec(raw), raw, account, "id2", 1, 1, "x")
    assert _modo(radice / result.content_dir) == 0o755
    assert _modo(radice / result.content_dir / "busta.eml") == 0o644


def test_i_file_sqlite_sono_leggibili_dal_gruppo(cfg, stack):
    """pecdesk apre l'archivio in sola lettura: senza il WAL non lo legge."""
    state, archive, _writer = stack
    for path in (state.path, archive.path):
        assert _modo(path) == 0o640
        wal = path.parent / f"{path.name}-wal"
        if wal.exists():
            assert _modo(wal) == 0o640


def test_divergenze_trovate_e_corrette(cfg, stack, account):
    _state, _archive, writer = stack
    raw = f.busta_trasporto()
    result = writer.write_message(parse_pec(raw), raw, account, "id3", 1, 1, "x")
    cartella = cfg.output_root / result.content_dir

    # come se l'avesse scritta una versione precedente
    os.chmod(cartella, 0o700)
    os.chmod(cartella / "busta.eml", 0o664)
    fuori = writer.divergenze()
    assert any("busta.eml" in riga for riga in fuori)

    assert writer.applica_permessi() == []
    assert _modo(cartella) == 0o750
    assert _modo(cartella / "busta.eml") == 0o640


def test_esiti_del_consumatore_non_viene_riscritta(cfg, stack):
    """`esiti/` è del consumatore: si sistema la cartella, non il contenuto."""
    _state, _archive, writer = stack
    esito = cfg.output_root / "esiti" / "2026-09-05.jsonl"
    esito.write_text('{"id": "x"}\n', encoding="utf-8")
    os.chmod(esito, 0o666)
    writer.applica_permessi()
    assert _modo(esito) == 0o666


def test_permessi_invalidi_rifiutati_in_configurazione(tmp_path, monkeypatch):
    from pecfetch.config import load_config

    monkeypatch.setenv("PECFETCH_PW_ROSSI", "x")
    path = tmp_path / "pecfetch.toml"
    path.write_text(
        f'[general]\noutput_root = "{tmp_path / "out"}"\n'
        f'state_dir = "{tmp_path / "stato"}"\n'
        '[permissions]\ndir_mode = "rwx"\n'
        '[[accounts]]\nid = "rossi"\naddress = "rossi@pec.it"\n'
        'host = "imap.example"\npassword_env = "PECFETCH_PW_ROSSI"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="permissions"):
        load_config(path)
