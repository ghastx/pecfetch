"""L'archivio storico: indirizzi confrontabili e intervalli di data corretti."""

import sqlite3

from pecfetch.archive import Archive


def _record(msg_id: str, sender: str, data: str, account: str = "rossi") -> dict:
    return {
        "id": msg_id,
        "casella": {"id": account, "cliente": "ROSSI", "etichetta": "Rossi",
                    "indirizzo": "rossi@pec.it"},
        "tipo": "posta_certificata",
        "certificato": True,
        "data": {"certificata": data, "invio": data},
        "acquisito_il": data,
        "mittente": {"indirizzo": sender, "nome": "", "dominio": sender.split("@")[-1]},
        "destinatari": ["rossi@pec.it"],
        "oggetto": "Avviso",
        "allegati": [],
        "message_id": f"<{msg_id}@pec.it>",
        "contenuto": {"cartella": f"coda/{msg_id}", "caratteri_corpo": 10},
    }


def test_indirizzi_scritti_minuscoli_anche_se_il_record_e_vecchio(tmp_path):
    with Archive(tmp_path / "archivio.sqlite3") as archive:
        archive.add(_record("a1", "ENTE@PEC.COMUNE.IT", "2026-09-05T10:00:00+02:00"),
                    "indice/2026-09-05.jsonl")
        riga = archive.db.execute(
            "SELECT from_addr, from_domain, to_addrs FROM messages").fetchone()
    assert riga["from_addr"] == "ente@pec.comune.it"
    assert riga["from_domain"] == "pec.comune.it"
    assert riga["to_addrs"] == "rossi@pec.it"


def test_migrazione_normalizza_le_righe_gia_in_archivio(tmp_path):
    """Finché restano com'erano, la storia del mittente non si trova più."""
    path = tmp_path / "archivio.sqlite3"
    with Archive(path) as archive:
        archive.add(_record("a1", "ente@pec.comune.it", "2026-09-05T10:00:00+02:00"),
                    "i.jsonl")
        # come se l'avesse scritta la versione 1: indirizzo verbatim
        archive.db.execute("UPDATE messages SET from_addr='ENTE@PEC.COMUNE.IT'")
        archive.db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")

    with Archive(path) as archive:
        riga = archive.db.execute("SELECT from_addr FROM messages").fetchone()
        versione = archive.db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert riga["from_addr"] == "ente@pec.comune.it"
    assert versione["value"] == "2"


def test_intervallo_until_comprende_tutto_il_giorno(tmp_path):
    with Archive(tmp_path / "archivio.sqlite3") as archive:
        archive.add(_record("a1", "e@pec.it", "2026-09-05T23:59:00+02:00"), "i.jsonl")
        archive.add(_record("a2", "e@pec.it", "2026-09-06T00:01:00+02:00"), "i.jsonl")
        dentro = archive.search(until="2026-09-05")
        oltre = archive.search(since="2026-09-06")
    assert [h.id for h in dentro] == ["a1"]
    assert [h.id for h in oltre] == ["a2"]


def test_sola_lettura_non_migra_niente(tmp_path):
    path = tmp_path / "archivio.sqlite3"
    with Archive(path) as archive:
        archive.add(_record("a1", "e@pec.it", "2026-09-05T10:00:00+02:00"), "i.jsonl")
    with Archive(path, read_only=True) as archive:
        assert archive.has("a1")
        try:
            archive.db.execute("UPDATE messages SET from_addr='x'")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower()
        else:                                   # pragma: no cover
            raise AssertionError("l'archivio in sola lettura non deve accettare scritture")
