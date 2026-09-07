"""L'unica email che pecdesk spedisce, e i limiti che ha per costruzione."""

from __future__ import annotations

import pytest

from pecdesk.config import DigestSettings
from pecdesk.mailer import MailError, build_message, send

CFG = DigestSettings(to="titolare@studio.it", sender="pecdesk@studio.it",
                     smtp_host="smtp.studio.it")


def test_un_solo_destinatario():
    message = build_message(CFG, "PEC 7 settembre", "testo")
    assert message.get_all("To") == ["titolare@studio.it"]


def test_piu_destinatari_rifiutati():
    cfg = DigestSettings(to="a@studio.it, b@studio.it", smtp_host="x")
    with pytest.raises(MailError, match="un solo destinatario"):
        build_message(cfg, "x", "y")


def test_senza_destinatario_non_si_spedisce():
    with pytest.raises(MailError, match="nessun destinatario"):
        build_message(DigestSettings(), "x", "y")


def test_non_e_una_risposta_a_niente():
    """Il programma non risponde a nessuno: nemmeno per sbaglio in un thread."""
    message = build_message(CFG, "PEC", "testo")
    assert message["In-Reply-To"] is None
    assert message["References"] is None
    assert message["Auto-Submitted"] == "auto-generated"


def test_destinatario_alterato_dopo_la_costruzione_blocca_l_invio():
    message = build_message(CFG, "PEC", "testo")
    del message["To"]
    message["To"] = "ladro@example.com"
    with pytest.raises(MailError, match="alterato"):
        send(CFG, message)


def test_testo_e_html_insieme():
    message = build_message(CFG, "PEC", "testo semplice", "<p>html</p>")
    tipi = {part.get_content_type() for part in message.walk()}
    assert "text/plain" in tipi and "text/html" in tipi
