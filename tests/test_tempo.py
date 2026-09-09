"""La convenzione sulle date: una sola, dichiarata, applicata ovunque."""

from datetime import datetime, timedelta, timezone

import pytest

import factories as f
from pecfetch import tempo
from pecfetch.config import ConfigError
from pecfetch.pec import parse_pec


def test_zona_sconosciuta_e_un_errore_non_un_ripiego():
    """Un refuso non deve diventare un silenzioso ritorno a UTC."""
    with pytest.raises(ValueError):
        tempo.zona("Europe/Nessunluogo")


def test_reso_porta_tutto_nel_fuso_dichiarato():
    tz = tempo.zona("Europe/Rome")
    utc = datetime(2026, 9, 5, 8, 22, 31, tzinfo=timezone.utc)
    altro = datetime(2026, 9, 5, 10, 22, 31,
                     tzinfo=timezone(timedelta(hours=2)))
    # stesso istante scritto in due modi: una sola resa
    assert tempo.reso(utc, tz) == "2026-09-05T10:22:31+02:00"
    assert tempo.reso(altro, tz) == tempo.reso(utc, tz)


def test_reso_ignora_quello_che_non_e_una_data():
    tz = tempo.zona("UTC")
    assert tempo.reso(None, tz) is None
    assert tempo.reso("cinque settembre", tz) is None


def test_una_data_senza_fuso_si_legge_come_utc():
    tz = tempo.zona("Europe/Rome")
    assert tempo.reso(datetime(2026, 1, 5, 12, 0), tz) == "2026-01-05T13:00:00+01:00"


def test_piu_recente_confronta_istanti_non_stringhe():
    """Regge anche le righe scritte in UTC prima di questa convenzione."""
    vecchia_utc = "2026-09-05T09:00:00+00:00"      # 11:00 a Roma
    nuova_roma = "2026-09-05T10:00:00+02:00"       # 08:00 UTC: precedente
    assert tempo.piu_recente(vecchia_utc, nuova_roma) == vecchia_utc
    # l'ordine alfabetico direbbe il contrario
    assert nuova_roma > vecchia_utc
    assert tempo.piu_recente("", nuova_roma) == nuova_roma
    assert tempo.piu_recente(nuova_roma, None) == nuova_roma


def test_le_tre_date_del_record_hanno_lo_stesso_offset(cfg, stack, account):
    """È il difetto segnalato dal collaudo: certificata +02:00, ricezione UTC."""
    _state, _archive, writer = stack
    raw = f.busta_trasporto()
    internaldate = datetime(2026, 9, 5, 8, 25, 0, tzinfo=timezone.utc)
    pm = parse_pec(raw, internaldate)
    record = writer.write_message(
        pm, raw, account, "id1", 1, 1, "2026-09-05T10:30:00+02:00"
    ).index_record

    offsets = {
        valore[-6:] for valore in record["data"].values() if valore
    } | {record["acquisito_il"][-6:]}
    assert offsets == {"+02:00"}
    assert record["data"]["ricezione"] == "2026-09-05T10:25:00+02:00"


def test_il_fuso_governa_anche_il_giorno_dell_indice(cfg, stack, account):
    """23:30 UTC del 5 è già il 6 a Roma: il nome del file lo dice."""
    _state, _archive, writer = stack
    tardi = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
    assert writer.index_path(tardi).name == "2026-09-06.jsonl"


def test_fuso_invalido_rifiutato_in_configurazione(tmp_path, monkeypatch):
    from pecfetch.config import load_config

    monkeypatch.setenv("PECFETCH_PW_ROSSI", "x")
    path = tmp_path / "pecfetch.toml"
    path.write_text(
        f'[general]\noutput_root = "{tmp_path / "out"}"\n'
        f'state_dir = "{tmp_path / "stato"}"\ntimezone = "Europe/Nessunluogo"\n'
        '[[accounts]]\nid = "rossi"\naddress = "rossi@pec.it"\n'
        'host = "imap.example"\npassword_env = "PECFETCH_PW_ROSSI"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="timezone"):
        load_config(path)
