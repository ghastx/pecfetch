"""Riconoscimento delle varie forme di busta PEC."""

import factories as f
import pytest

from pecfetch.pec import (
    RECEIPTS_NEGATIVE,
    RECEIPTS_POSITIVE,
    is_output_type,
    parse_daticert,
    parse_pec,
)


def test_busta_di_trasporto_estrae_il_messaggio_vero():
    pm = parse_pec(f.busta_trasporto())
    assert pm.msg_type == "posta_certificata"
    assert pm.certified is True
    # Mittente e oggetto vengono da postacert.eml, non dagli header della busta.
    assert pm.from_addr["address"] == "ente@pec.comune.it"
    assert pm.from_addr["domain"] == "pec.comune.it"
    assert pm.subject == "Avviso di accertamento n. 123/2026"
    assert "Si notifica l'atto" in pm.body_text
    assert pm.envelope_subject.startswith("POSTA CERTIFICATA")
    assert pm.gestore == "Aruba PEC S.p.A."
    assert pm.pec_identifier.startswith("opec")
    assert pm.date_certified.isoformat() == "2026-09-05T10:22:31+02:00"
    assert pm.postacert_raw and pm.daticert_raw
    assert pm.flags == []


def test_busta_con_allegati():
    pm = parse_pec(f.busta_trasporto(postacert=f.inner_message(
        attachments=[("Atto n° 1/2026.pdf", b"%PDF-1.4 finto", "pdf"),
                     ("prospetto.xlsx", b"PK\x03\x04finto", "octet-stream")],
    )))
    assert [a.filename for a in pm.attachments] == ["Atto n° 1/2026.pdf", "prospetto.xlsx"]
    assert pm.attachments[0].content_type == "application/pdf"
    # La firma della busta non è un allegato utile.
    assert not any("p7s" in a.filename for a in pm.attachments)
    # E nemmeno daticert.xml o postacert.eml.
    assert not any(a.filename in ("daticert.xml", "postacert.eml") for a in pm.attachments)


def test_busta_non_firmata_funziona_uguale():
    pm = parse_pec(f.busta_trasporto(signed=False))
    assert pm.msg_type == "posta_certificata"
    assert pm.subject == "Avviso di accertamento n. 123/2026"


def test_busta_di_anomalia_non_certificata():
    pm = parse_pec(f.busta_anomalia())
    assert pm.msg_type == "busta_anomalia"
    assert pm.certified is False
    assert pm.from_addr["address"] == "fornitore@gmail.com"
    assert pm.subject == "Preventivo"
    assert is_output_type(pm.msg_type)


def test_busta_senza_postacert_degrada():
    pm = parse_pec(f.busta_trasporto(postacert=None if False else b""))
    # postacert vuoto: si usa la busta, ma il problema resta annotato.
    assert pm.msg_type == "posta_certificata"
    assert "postacert_missing" in pm.flags


@pytest.mark.parametrize("tipo,atteso", [
    ("accettazione", "accettazione"),
    ("presa-in-carico", "presa_in_carico"),
    ("avvenuta-consegna", "avvenuta_consegna"),
])
def test_ricevute_positive_sono_rumore(tipo, atteso):
    pm = parse_pec(f.ricevuta(tipo))
    assert pm.msg_type == atteso
    assert pm.receipt_class == "positiva"
    assert atteso in RECEIPTS_POSITIVE
    assert not is_output_type(pm.msg_type)


@pytest.mark.parametrize("tipo,atteso", [
    ("errore-consegna", "errore_consegna"),
    ("non-accettazione", "non_accettazione"),
    ("preavviso-errore-consegna", "preavviso_errore_consegna"),
    ("rilevazione-virus", "rilevazione_virus"),
])
def test_ricevute_negative_vanno_in_output(tipo, atteso):
    pm = parse_pec(f.ricevuta(tipo, errore="no-dest",
                              errore_esteso="casella del destinatario inesistente"))
    assert pm.msg_type == atteso
    assert pm.receipt_class == "negativa"
    assert atteso in RECEIPTS_NEGATIVE
    assert is_output_type(pm.msg_type)
    # Collegate al messaggio originale dello studio.
    assert pm.ref_message_id == "<originale-studio@pec.it>"
    assert "inesistente" in pm.error_detail


def test_messaggio_generico():
    pm = parse_pec(f.messaggio_semplice())
    assert pm.msg_type == "generico"
    assert pm.certified is False
    assert is_output_type(pm.msg_type)


def test_classificazione_dagli_header_senza_daticert():
    raw = f.ricevuta("avvenuta-consegna").replace(
        b"<postacert tipo=", b"<rotto tipo="
    )
    pm = parse_pec(raw)
    assert pm.msg_type == "avvenuta_consegna"  # ripiego su X-Ricevuta


def test_daticert_malformato_recuperato():
    dc = parse_daticert(f.daticert_grezzo())
    assert dc is not None
    assert dc.tipo == "avvenuta-consegna"
    assert dc.mittente == "studio@pec.it"
    assert dc.gestore == "Namirial S.p.A."
    assert dc.data.hour == 9


def test_daticert_illeggibile_non_ferma_il_parsing():
    raw = f.busta_trasporto(dati=b"questo non e' xml")
    pm = parse_pec(raw)
    assert pm.msg_type == "posta_certificata"   # ripiego su X-Trasporto
    assert "daticert_unparsable" in pm.flags


def test_oggetto_rfc2047_e_charset_bugiardo():
    inner = f.inner_message(
        subject="=?iso-8859-1?Q?Comunicazione_perch=E9_urgente?=",
        body="Città di Verona: accertamento perché dovuto.",
        charset="utf-8",
    )
    pm = parse_pec(f.busta_trasporto(postacert=inner))
    assert pm.subject == "Comunicazione perché urgente"
    assert "perché dovuto" in pm.body_text


def test_solo_html_viene_convertito():
    inner = f.inner_message(
        body="", html="<html><body><p>Primo</p><p>Secondo</p></body></html>",
    )
    pm = parse_pec(f.busta_trasporto(postacert=inner))
    assert pm.body_source == "html"
    assert "Primo" in pm.body_text and "<p>" not in pm.body_text


def test_alternative_preferisce_il_testo():
    inner = f.inner_message(body="versione testo", html="<p>versione html</p>")
    pm = parse_pec(f.busta_trasporto(postacert=inner))
    assert pm.body_source == "text"
    assert pm.body_text == "versione testo"


def test_bytes_completamente_rotti():
    pm = parse_pec(b"\x00\x01\x02 non e' un messaggio")
    assert pm.msg_type == "generico"
    assert pm.body_text is not None


def test_messaggio_vuoto():
    pm = parse_pec(b"")
    assert pm.msg_type == "generico"


def test_postacert_in_base64_viene_decodificato():
    """Content-Type: message/rfc822 + base64: illegale ma reale."""
    pm = parse_pec(f.busta_trasporto())
    assert "Content-Type" not in pm.body_text
    assert pm.body_text.startswith("Si notifica")


def test_indirizzi_sempre_minuscoli_ma_l_originale_resta():
    """Sulle caselle vere arriva LASERMARCSRL@PEC.IT per una casella minuscola."""
    inner = f.inner_message(sender="Mario.Rossi@PEC.IT", to="LASERMARCSRL@PEC.IT")
    pm = parse_pec(f.busta_trasporto(postacert=inner, to="LASERMARCSRL@PEC.IT"))
    assert pm.from_addr["address"] == "mario.rossi@pec.it"
    assert pm.from_addr["domain"] == "pec.it"
    assert [d["address"] for d in pm.to] == ["lasermarcsrl@pec.it"]
    # la forma scritta dal gestore non si perde: sta negli header conservati
    assert pm.headers["postacert.From"] == "Mario.Rossi@PEC.IT"
    assert pm.headers["To"] == "LASERMARCSRL@PEC.IT"


def test_indirizzi_del_daticert_normalizzati():
    dati = f.daticert(mittente="ENTE@PEC.COMUNE.IT",
                      destinatario="LASERMARCSRL@PEC.IT")
    pm = parse_pec(f.busta_trasporto(dati=dati))
    assert pm.daticert.mittente == "ente@pec.comune.it"
    assert [d["address"] for d in pm.daticert.destinatari] == ["lasermarcsrl@pec.it"]
