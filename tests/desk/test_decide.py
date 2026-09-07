"""L'ordine delle precedenze. È il cuore del programma e non chiama niente."""

from __future__ import annotations

from desk.fabbrica import make_attachment, write_message

from pecdesk.config import MaterialLimits
from pecdesk.decide import compose
from pecdesk.material import build as build_material
from pecdesk.model import Judgment
from pecdesk.queue import read_item
from pecdesk.rules import apply_rules
from pecdesk.signals import History, collect

ORA = "2026-09-07T07:15:00+02:00"
MATURO = History(seen_on_mailbox=50, seen_anywhere=80, mailbox_total=300,
                 coverage_days=400, sufficient=True)
NUOVO = History(seen_on_mailbox=0, seen_anywhere=0, mailbox_total=300,
                coverage_days=400, sufficient=True)


def _prepare(queue_dir, directives, history=MATURO, **kwargs):
    write_message(queue_dir, kwargs.pop("msg_id", "m1"), **kwargs)
    item = read_item(sorted(queue_dir.iterdir())[-1])
    material = build_material(item, MaterialLimits(), directives.keywords)
    signals = collect(item, history, material.scan_text)
    rules = apply_rules(directives, item)
    return item, rules, signals, material


def _compose(queue_dir, directives, judgment, **kwargs):
    history = kwargs.pop("history", MATURO)
    item, rules, signals, material = _prepare(queue_dir, directives,
                                              history=history, **kwargs)
    return compose(item, rules, signals, judgment, material, directives, ORA)


# -- 2. il veto di sicurezza prevale su tutto --------------------------------

def test_il_sospetto_vieta_l_inoltro_anche_contro_una_regola(queue_dir, directives):
    """Una regola deterministica direbbe 'in contabilità'. Il veto no."""
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="fattura", route="studio", recipient_alias="contabilita",
                 confidence="alta", reason="fattura"),
        sender="fornitore@pec.it",       # la regola 'fornitore-noto' fa match
        attachments=[make_attachment("fattura.zip", text="",
                                     content_type="application/zip",
                                     status="unsupported_archive", method="archive",
                                     suspicious=True, note="nome ingannevole")],
    )
    assert "fornitore-noto" in outcome.rules_applied     # la regola ha fatto match
    assert outcome.klass == "sospetto"
    assert outcome.route == "titolare"                   # ...ma non decide
    assert outcome.route_origin == "veto"
    assert outcome.recipient == "titolare@studio.it"
    assert outcome.owner_attention is True


def test_il_solo_tentativo_di_istruzione_basta_al_veto(queue_dir, directives):
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="fattura", route="studio", recipient_alias="contabilita",
                 confidence="alta", instruction_attempt=True),
        sender="fornitore@pec.it",
    )
    assert outcome.klass == "sospetto"
    assert outcome.route == "titolare"
    assert "tentata_istruzione" in outcome.suspicion_signals


def test_il_sospetto_e_il_massimo_non_la_media(queue_dir, directives):
    """Il codice non vede niente, il modello sì: vince il modello."""
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="sollecito_pagamento", route="studio",
                 recipient_alias="contabilita", suspicion="probabile",
                 suspicion_signals=["coordinate bancarie cambiate"],
                 confidence="alta"),
        sender="fornitore@pec.it",
    )
    assert outcome.suspicion_level == "probabile"
    assert outcome.route == "titolare"
    assert "modello:coordinate bancarie cambiate" in outcome.suspicion_signals


# -- 3. la regola prevale sul modello ---------------------------------------

def test_la_regola_batte_il_giudizio_del_modello(queue_dir, directives):
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="contributi_inps", route="studio",
                 recipient_alias="contabilita", confidence="alta"),
        sender="notifiche@postacert.inps.gov.it",
    )
    assert outcome.route_origin == "regola"
    assert outcome.recipient_alias == "paghe"           # non contabilita
    assert outcome.recipient == "paghe@studio.it"


def test_senza_modello_la_regola_basta(queue_dir, directives):
    """Le ricevute negative sono un fatto: nessuna chiamata, nessun dubbio."""
    outcome = _compose(queue_dir, directives, None, msg_type="errore_consegna")
    assert outcome.route == "studio"
    assert outcome.recipient_alias == "segreteria"
    assert outcome.route_origin == "regola"
    assert outcome.klass == "inoltro"
    assert outcome.uncertain is False


# -- 4. i tipi che vanno sempre al titolare ----------------------------------

def test_il_tipo_manda_al_titolare_anche_senza_termine_visto(queue_dir, directives):
    """Sull'estratto il silenzio non prova niente: il tipo basta a salire."""
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="cartella_esattoriale", route="nessuno",
                 confidence="alta", deadline_present=False),
        sender="sconosciuto@pec.it", history=MATURO,
    )
    assert outcome.route == "titolare"
    assert outcome.route_origin == "tipo"
    assert outcome.owner_attention is True
    assert outcome.deadline is not None
    assert outcome.deadline["verificato"] is False
    assert outcome.deadline["origine"] == "tipo_documento"


def test_una_regola_puo_instradare_ma_non_cancellare_l_attenzione(queue_dir, directives):
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="avviso_accertamento", route="nessuno", confidence="alta"),
        sender="x@postacert.inps.gov.it",     # regola: studio/paghe
    )
    assert outcome.route == "studio"          # la regola decide l'instradamento
    assert outcome.route_origin == "regola"
    assert outcome.owner_attention is True    # ...ma l'attenzione resta
    assert outcome.klass == "attenzione"


# -- 6. l'incertezza non si maschera da decisione ----------------------------

def test_confidenza_bassa_finisce_al_titolare(queue_dir, directives):
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="altro", route="studio", recipient_alias="contabilita",
                 confidence="bassa", uncertainty=["documento_ambiguo"]),
        sender="sconosciuto2@pec.it",
    )
    assert outcome.uncertain is True
    assert outcome.route == "titolare"
    assert outcome.route_origin == "incertezza"
    assert "documento_ambiguo" in outcome.uncertainty


def test_instradamento_assente_finisce_al_titolare(queue_dir, directives):
    outcome = _compose(queue_dir, directives,
                       Judgment(doc_type="altro", route="", confidence="alta"))
    assert outcome.route == "titolare"
    assert "instradamento_non_determinato" in outcome.uncertainty


def test_alias_sconosciuto_non_diventa_un_inoltro(queue_dir, directives):
    """Nessuna frase dentro un messaggio può far comparire un destinatario nuovo."""
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="fattura", route="studio",
                 recipient_alias="ladro-esterno", confidence="alta"),
    )
    assert outcome.route == "titolare"
    assert outcome.recipient == "titolare@studio.it"
    assert outcome.uncertain is True


# -- 7. il termine ------------------------------------------------------------

def test_termine_da_ocr_non_e_verificato_e_sale_al_titolare(queue_dir, directives):
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="notifica_ente", route="studio",
                 recipient_alias="contabilita", confidence="alta",
                 deadline_present=True, deadline_date="03/10/2026",
                 deadline_what="ricorso"),
        attachments=[make_attachment("scansione.pdf", method="pdf_ocr",
                                     text="Il termine per il ricorso scade il "
                                          "03/10/2026")],
    )
    assert outcome.deadline["verificato"] is False
    assert outcome.deadline["ocr"] is True
    assert outcome.owner_attention is True
    assert "ocr_incerto" in outcome.uncertainty
    assert outcome.confidence == "media"       # l'OCR abbassa il tetto


def test_termine_leggibile_resta_verificato(queue_dir, directives):
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="notifica_ente", route="studio",
                 recipient_alias="contabilita", confidence="alta",
                 deadline_present=True, deadline_date="2026-10-03",
                 deadline_what="ricorso"),
        body="Il termine per il ricorso scade il 3 ottobre 2026.",
    )
    assert outcome.deadline["verificato"] is True
    assert outcome.deadline["data"] == "2026-10-03"


# -- l'esito ordinario --------------------------------------------------------

def test_un_inoltro_ordinario_resta_un_inoltro(queue_dir, directives):
    outcome = _compose(
        queue_dir, directives,
        Judgment(doc_type="fattura", route="studio", recipient_alias="contabilita",
                 confidence="alta", reason="fattura di fornitore abituale"),
        sender="fornitore@pec.it",
    )
    assert outcome.klass == "inoltro"
    assert outcome.recipient == "contabilita@studio.it"
    assert outcome.uncertain is False
    assert outcome.owner_attention is False


def test_niente_giudizio_e_niente_regole_non_produce_una_classificazione(queue_dir,
                                                                        directives):
    """Degradare, non fingere: un esito non lavorato si dichiara tale."""
    item, rules, signals, material = _prepare(queue_dir, directives,
                                              sender="sconosciuto@pec.it")
    outcome = compose(item, rules, signals, None, material, directives, ORA,
                      error="API irraggiungibile")
    assert outcome.klass == "non_classificato"
    assert outcome.route == "titolare"
    assert outcome.error == "API irraggiungibile"
    assert outcome.uncertain is True


def test_l_esito_porta_sempre_l_impronta_delle_direttive(queue_dir, directives):
    outcome = _compose(queue_dir, directives,
                       Judgment(doc_type="fattura", route="nessuno",
                                confidence="alta"))
    assert outcome.directives["versione"] == "test-1"
    assert outcome.directives["regole"].startswith("sha256:")
    assert outcome.directives["istruzioni"].startswith("sha256:")
