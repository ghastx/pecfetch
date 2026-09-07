"""Lo stato: prenotare prima di produrre, e non produrre due volte."""

from __future__ import annotations

from pecdesk.state import ARCHIVED, SUSPENDED, State


def _state(tmp_path):
    return State(tmp_path / "stato.sqlite3")


def test_un_messaggio_lavorato_non_si_rilavora(tmp_path):
    with _state(tmp_path) as state:
        assert state.claim("m1", "rossi").proceed is True
        state.mark_classified("m1", "2026-09-07.jsonl", "inoltro")
        state.mark_archived("m1")
        claim = state.claim("m1", "rossi")
        assert claim.proceed is False
        assert claim.state == ARCHIVED


def test_classificato_ma_non_spostato_va_solo_spostato(tmp_path):
    """Interruzione fra la riga di esito e il rename: non si riclassifica."""
    with _state(tmp_path) as state:
        state.claim("m1")
        state.mark_classified("m1", "2026-09-07.jsonl", "inoltro")
        claim = state.claim("m1")
        assert claim.proceed is False
        assert claim.needs_move is True
        assert state.classified_not_archived() == ["m1"]


def test_riconciliazione_recupera_un_esito_scritto_senza_stato(tmp_path):
    """Un esito su disco è la prova che il messaggio è stato lavorato."""
    with _state(tmp_path) as state:
        state.claim("m1")                       # in_corso, poi il processo muore
        fixed = state.reconcile({"m1", "m2"})
        assert set(fixed) == {"m1", "m2"}
        assert state.claim("m1").proceed is False
        assert state.claim("m2").proceed is False


def test_la_riconciliazione_non_tocca_chi_e_gia_archiviato(tmp_path):
    with _state(tmp_path) as state:
        state.claim("m1")
        state.mark_classified("m1", "f.jsonl")
        state.mark_archived("m1")
        assert state.reconcile({"m1"}) == []
        assert state.get("m1")["stato"] == ARCHIVED


def test_i_tentativi_si_esauriscono_e_il_messaggio_resta_in_coda(tmp_path):
    with _state(tmp_path) as state:
        for _ in range(3):
            assert state.claim("m1", max_attempts=3).proceed is True
            state.fail("m1", "API irraggiungibile", max_attempts=3)
        assert state.get("m1")["stato"] == SUSPENDED
        claim = state.claim("m1", max_attempts=3)
        assert claim.proceed is False
        assert "sospeso" in claim.reason


def test_riprova_sblocca_un_sospeso(tmp_path):
    with _state(tmp_path) as state:
        state.claim("m1")
        state.suspend("m1", "illeggibile")
        assert state.claim("m1").proceed is False
        assert state.retry("m1") is True
        assert state.claim("m1").proceed is True


# -- il riepilogo si prenota prima di partire --------------------------------

def test_il_riepilogo_si_prenota_una_volta_sola(tmp_path):
    with _state(tmp_path) as state:
        assert state.claim_digest("2026-09-07", "t@studio.it") is True
        assert state.claim_digest("2026-09-07", "t@studio.it") is False


def test_una_prenotazione_mai_spedita_si_rilascia(tmp_path):
    """Se l'SMTP muore prima di spedire non si è prodotto niente: si riprova."""
    with _state(tmp_path) as state:
        state.claim_digest("2026-09-07", "t@studio.it")
        state.release_digest("2026-09-07")
        assert state.claim_digest("2026-09-07", "t@studio.it") is True


def test_un_riepilogo_gia_spedito_non_si_rilascia(tmp_path):
    """L'effetto è già uscito: rilasciarlo significherebbe spedirlo due volte."""
    with _state(tmp_path) as state:
        state.claim_digest("2026-09-07", "t@studio.it")
        state.mark_digest_sent("2026-09-07", "{}")
        state.release_digest("2026-09-07")
        assert state.claim_digest("2026-09-07", "t@studio.it") is False
        assert state.digest_row("2026-09-07")["sent_at"] is not None


def test_forza_permette_di_rispedire(tmp_path):
    with _state(tmp_path) as state:
        state.claim_digest("2026-09-07", "t@studio.it")
        state.mark_digest_sent("2026-09-07", "{}")
        state.force_digest("2026-09-07")
        assert state.claim_digest("2026-09-07", "t@studio.it") is True
