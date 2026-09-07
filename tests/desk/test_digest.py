"""Il riepilogo: trenta secondi da un telefono, e mai un silenzio."""

from __future__ import annotations

from datetime import date

from pecdesk.digest import Unworked, build, render_html, render_text, short_date
from pecdesk.outcomes import Outcome


def _o(msg_id, **kwargs):
    base = dict(id=msg_id, processed_at="2026-09-07T07:15:00+02:00",
                account="rossi", client="ROSSI", sender="x@pec.it",
                subject="Oggetto", doc_type="fattura", klass="ordinario",
                route="nessuno", reason="ordinaria", confidence="alta",
                date="2026-09-07T08:00:00+02:00")
    base.update(kwargs)
    return Outcome(**base)


def test_ogni_messaggio_compare_in_una_sezione_sola():
    outcomes = [
        _o("a", klass="sospetto", suspicion_level="probabile",
           suspicion_signals=["mittente_mai_visto"]),
        _o("b", klass="attenzione", deadline={"presente": True, "data": "2026-10-03",
                                              "verificato": True}),
        _o("c", klass="attenzione", owner_attention=True, route="titolare"),
        _o("d", klass="inoltro", route="studio", recipient_alias="contabilita"),
        _o("e"),
    ]
    digest = build(outcomes, day=date(2026, 9, 7))
    assert [o.id for o in digest.suspicious] == ["a"]
    assert [o.id for o in digest.deadlines] == ["b"]
    assert [o.id for o in digest.attention] == ["c"]
    assert [o.id for o in digest.forwards] == ["d"]
    assert [o.id for o in digest.ordinary] == ["e"]
    assert digest.total == 5


def test_i_termini_sono_ordinati_per_data_e_gli_incerti_in_fondo():
    outcomes = [
        _o("tardi", deadline={"presente": True, "data": "2026-12-01",
                              "verificato": True}),
        _o("ignoto", deadline={"presente": True, "data": None,
                               "verificato": False}),
        _o("presto", deadline={"presente": True, "data": "2026-10-03",
                               "verificato": True}),
    ]
    digest = build(outcomes, day=date(2026, 9, 7))
    assert [o.id for o in digest.deadlines] == ["presto", "tardi", "ignoto"]


def test_un_termine_non_verificato_si_vede():
    digest = build([_o("x", deadline={"presente": True, "data": "03/10/2026",
                                      "verificato": False})],
                   day=date(2026, 9, 7))
    testo = render_text(digest)
    assert "03/10?" in testo               # il punto interrogativo è il punto


def test_un_termine_senza_data_dice_da_verificare():
    digest = build([_o("x", deadline={"presente": True, "data": None,
                                      "verificato": False,
                                      "cosa": "possibile termine"})],
                   day=date(2026, 9, 7))
    assert "da verificare" in render_text(digest)


def test_l_oggetto_dell_email_dice_subito_cosa_conta():
    digest = build([
        _o("a", klass="sospetto", suspicion_level="probabile"),
        _o("b", deadline={"presente": True, "data": "2026-10-03", "verificato": True}),
    ], day=date(2026, 9, 7))
    assert digest.subject_line() == "PEC 7 settembre — 1 sospetto, 1 termine"


def test_i_non_lavorati_compaiono_sempre():
    """Una mattina senza email è peggio di una mattina con 'non classificati'."""
    digest = build([], [Unworked(id="x", mailbox="ROSSI", sender="a@pec.it",
                                 subject="Avviso", reason="API irraggiungibile")],
                   day=date(2026, 9, 7), notes=["classificazione interrotta"])
    testo = render_text(digest)
    assert "NON LAVORATI (1)" in testo
    assert "API irraggiungibile" in testo
    assert "! classificato" in testo or "classificazione interrotta" in testo


def test_un_esito_non_classificato_finisce_fra_i_non_lavorati():
    digest = build([_o("x", klass="non_classificato", error="modello assente")],
                   day=date(2026, 9, 7))
    assert digest.suspicious == [] and digest.ordinary == []
    assert digest.unworked[0].reason == "modello assente"


def test_coda_vuota_lo_dice():
    assert "Niente in coda" in render_text(build([], day=date(2026, 9, 7)))


def test_il_testo_resta_stretto_per_un_telefono():
    outcomes = [_o(str(i), klass="inoltro", route="studio",
                   recipient_alias="contabilita",
                   subject="Oggetto molto lungo " * 10) for i in range(5)]
    righe = render_text(build(outcomes, day=date(2026, 9, 7))).splitlines()
    assert max(len(r) for r in righe) <= 78


def test_html_e_testo_dicono_le_stesse_cose():
    outcomes = [_o("a", klass="sospetto", suspicion_level="probabile",
                   suspicion_signals=["allegato_compresso"])]
    digest = build(outcomes, day=date(2026, 9, 7))
    html = render_html(digest)
    assert "Sospetti (1)" in html
    assert "allegato_compresso" in html
    assert "niente è stato inoltrato" in html


def test_html_neutralizza_il_contenuto_ostile():
    digest = build([_o("a", subject="<script>alert(1)</script>", klass="inoltro",
                       route="studio", recipient_alias="contabilita")],
                   day=date(2026, 9, 7))
    html = render_html(digest)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_data_breve():
    assert short_date("2026-10-03") == "03/10"
    assert short_date("03/10/2026") == "03/10"
    assert short_date("") == ""


def test_le_date_scritte_a_parole_si_leggono():
    assert short_date("30 settembre 2026") == "30/09"
    assert short_date("entro il 3 Ottobre 2026") == "03/10"


def test_una_data_incomprensibile_non_viene_tagliata_a_meta():
    assert short_date("prossima udienza") == "prossima udienza"


def test_i_termini_a_parole_si_ordinano_con_gli_altri():
    outcomes = [
        _o("dicembre", deadline={"presente": True, "data": "1 dicembre 2026",
                                 "verificato": True}),
        _o("ottobre", deadline={"presente": True, "data": "2026-10-03",
                                "verificato": True}),
    ]
    digest = build(outcomes, day=date(2026, 9, 7))
    assert [o.id for o in digest.deadlines] == ["ottobre", "dicembre"]


def test_l_etichetta_della_regola_parla_meglio_di_altro():
    """Quando una regola decide da sola non c'è tipo di documento: l'etichetta
    che il titolare ha scritto nelle regole dice più di 'altro'."""
    digest = build([_o("x", doc_type="altro", label="invio-fallito",
                       klass="attenzione", owner_attention=True,
                       route="titolare")], day=date(2026, 9, 7))
    testo = render_text(digest)
    assert "invio fallito" in testo
    assert "— altro" not in testo
