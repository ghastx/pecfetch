"""Logica di stato: il criterio di 'già scaricato' non dipende dai flag IMAP."""

from pecfetch.state import (
    STATUS_DONE,
    STATUS_SKIPPED,
    State,
    content_hash,
    message_key,
)


def test_cursore_parte_da_zero(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        cur = state.get_cursor("rossi", "INBOX")
        assert cur.uidvalidity is None and cur.last_uid == 0
        assert cur.initialized is False


def test_cursore_non_torna_indietro(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        state.advance_uid("rossi", "INBOX", 100, 10)
        state.advance_uid("rossi", "INBOX", 100, 5)
        assert state.get_cursor("rossi", "INBOX").last_uid == 10


def test_uidvalidity_cambiata_invalida_gli_uid(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        state.advance_uid("rossi", "INBOX", 100, 42, "2026-09-01T10:00:00+00:00")
        state.reset_uidvalidity("rossi", "INBOX", 200)
        cur = state.get_cursor("rossi", "INBOX")
        assert cur.uidvalidity == 200
        assert cur.last_uid == 0
        # la data dell'ultimo messaggio visto resta: serve a risincronizzare
        assert cur.last_seen_date is not None


def test_advance_con_uidvalidity_diversa_azzera(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        state.advance_uid("rossi", "INBOX", 100, 42)
        state.advance_uid("rossi", "INBOX", 200, 3)
        cur = state.get_cursor("rossi", "INBOX")
        assert cur.uidvalidity == 200 and cur.last_uid == 3


def test_deduplica_per_contenuto_e_per_casella(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        digest = content_hash(b"contenuto")
        state.begin_message("rossi", 1, 5, "k1", digest, "posta_certificata", "<a>", "og")
        state.finish_message("rossi", 1, 5, STATUS_DONE)
        assert state.find_by_hash("rossi", digest) is not None
        # Lo stesso contenuto su un'altra casella è un altro messaggio:
        # due clienti diversi possono ricevere la stessa PEC.
        assert state.find_by_hash("bianchi", digest) is None


def test_messaggio_in_sospeso_non_conta_come_scaricato(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        digest = content_hash(b"x")
        state.begin_message("rossi", 1, 5, "k", digest, "generico", "<a>", "og")
        assert state.find_by_hash("rossi", digest) is None
        state.finish_message("rossi", 1, 5, STATUS_SKIPPED)
        assert state.find_by_hash("rossi", digest) is not None


def test_tentativi_incrementano(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        digest = content_hash(b"x")
        assert state.begin_message("rossi", 1, 5, "k", digest, "g", "<a>", "o") == 1
        assert state.begin_message("rossi", 1, 5, "k", digest, "g", "<a>", "o") == 2


def test_fasi_indipendenti(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        state.begin_message("rossi", 1, 5, "k", "h", "g", "<a>", "o")
        state.mark_phase("rossi", 1, 5, "files", "coda/x")
        row = state.get_message("rossi", 1, 5)
        assert row["files_done"] == 1 and row["index_done"] == 0
        assert row["content_dir"] == "coda/x"
        state.mark_phase("rossi", 1, 5, "index", "indice/2026-09-05.jsonl")
        row = state.get_message("rossi", 1, 5)
        assert row["index_done"] == 1
        assert row["index_file"] == "indice/2026-09-05.jsonl"


def test_ricevute_registrate_senza_duplicati(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        for _ in range(2):
            state.record_receipt("m1", "rossi", "accettazione", "<rif>", "og", "Aruba", None)
        assert state.stats()["receipts"] == 1


def test_chiave_messaggio_stabile_e_per_casella():
    digest = content_hash(b"contenuto")
    assert message_key("rossi", digest) == message_key("rossi", digest)
    assert message_key("rossi", digest) != message_key("bianchi", digest)
    assert len(message_key("rossi", digest)) == 24


def test_diario_esecuzioni(tmp_path):
    with State(tmp_path / "s.sqlite3") as state:
        run_id = state.start_run("run")
        state.log_error(run_id, "rossi", "imap", "timeout")
        state.finish_run(run_id, 1, 1, 3, 2, 2)
        run = state.last_runs(1)[0]
        assert run["accounts_err"] == 1 and run["exit_code"] == 2
        assert state.run_errors(run_id)[0]["message"] == "timeout"
