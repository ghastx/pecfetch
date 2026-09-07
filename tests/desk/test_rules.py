"""Le regole deterministiche: certezze, applicate senza chiedere niente a nessuno."""

from __future__ import annotations

import pytest
from desk.fabbrica import make_attachment, write_message

from pecdesk.directives import DirectivesError, load_directives
from pecdesk.queue import read_item
from pecdesk.rules import apply_rules


def _item(queue_dir, **kwargs):
    write_message(queue_dir, kwargs.pop("msg_id", "m1"), **kwargs)
    return read_item(sorted(queue_dir.iterdir())[-1])


def test_dominio_decide_e_porta_l_etichetta(queue_dir, directives):
    item = _item(queue_dir, sender="notifiche@agenziariscossione.gov.it")
    match = apply_rules(directives, item)
    assert match.route == "titolare"
    assert match.attention is True
    assert match.label == "riscossione"
    assert match.deciding == "riscossione"


def test_il_dominio_dichiarato_copre_i_sottodomini(queue_dir, directives):
    item = _item(queue_dir, sender="x@notifiche.agenziariscossione.gov.it")
    assert apply_rules(directives, item).route == "titolare"


def test_dominio_simile_ma_diverso_non_fa_match(queue_dir, directives):
    # "agenziariscossione.gov.it.example.com" non è il dominio dell'ente
    item = _item(queue_dir, sender="x@agenziariscossione.gov.it.example.com")
    assert apply_rules(directives, item).route == ""


def test_priorita_decide_quando_due_regole_valgono(queue_dir, directives):
    item = _item(queue_dir, sender="x@postacert.inps.gov.it",
                 msg_type="errore_consegna")
    match = apply_rules(directives, item)
    # inps ha priorità 90, ricevute-negative 80: decide inps
    assert match.deciding == "inps"
    assert match.recipient_alias == "paghe"
    assert set(match.matched) == {"inps", "ricevute-negative"}


def test_salta_modello_evita_la_chiamata(queue_dir, directives):
    item = _item(queue_dir, msg_type="errore_consegna")
    match = apply_rules(directives, item)
    assert match.skip_model is True
    assert match.recipient_alias == "segreteria"


def test_nessuna_regola_nessun_instradamento(queue_dir, directives):
    item = _item(queue_dir, sender="sconosciuto@pec.it")
    match = apply_rules(directives, item)
    assert match.any is False
    assert match.route == ""


def test_condizioni_in_and(tmp_path, queue_dir):
    (tmp_path / "r.toml").write_text("""
versione = "t"
[destinatari]
titolare = "t@studio.it"
contabilita = "c@studio.it"

[[regola]]
id = "solo-con-zip-da-rossi"
quando = { casella = ["rossi"], allegato_estensione = ["zip"] }
allora = { instradamento = "titolare" }
""", encoding="utf-8")
    (tmp_path / "i.md").write_text("niente", encoding="utf-8")
    directives = load_directives(tmp_path / "r.toml", tmp_path / "i.md")

    senza = _item(queue_dir, msg_id="a")
    assert apply_rules(directives, senza).route == ""

    con = _item(queue_dir, msg_id="b", attachments=[
        make_attachment("all.zip", content_type="application/zip", text="")])
    assert apply_rules(directives, con).route == "titolare"


# -- validazione delle direttive: meglio non lavorare che lavorare a caso ----

def _load(tmp_path, rules: str):
    (tmp_path / "r.toml").write_text(rules, encoding="utf-8")
    (tmp_path / "i.md").write_text("x", encoding="utf-8")
    return load_directives(tmp_path / "r.toml", tmp_path / "i.md")


def test_serve_sempre_un_titolare(tmp_path):
    with pytest.raises(DirectivesError, match="titolare"):
        _load(tmp_path, '[destinatari]\ncontabilita = "c@studio.it"\n')


def test_instradamento_sconosciuto_rifiutato(tmp_path):
    with pytest.raises(DirectivesError, match="instradamento"):
        _load(tmp_path, """
[destinatari]
titolare = "t@studio.it"
[[regola]]
id = "x"
allora = { instradamento = "cestino" }
""")


def test_destinatario_inventato_rifiutato(tmp_path):
    with pytest.raises(DirectivesError, match="alias"):
        _load(tmp_path, """
[destinatari]
titolare = "t@studio.it"
[[regola]]
id = "x"
allora = { instradamento = "studio", a = "reparto-che-non-esiste" }
""")


def test_condizione_sconosciuta_rifiutata(tmp_path):
    with pytest.raises(DirectivesError, match="condizioni sconosciute"):
        _load(tmp_path, """
[destinatari]
titolare = "t@studio.it"
[[regola]]
id = "x"
quando = { colore_della_busta = ["verde"] }
allora = { instradamento = "titolare" }
""")


def test_tipo_sempre_al_titolare_deve_esistere(tmp_path):
    with pytest.raises(DirectivesError, match="tipi_sempre_al_titolare"):
        _load(tmp_path, """
tipi_documento = ["fattura", "altro"]
tipi_sempre_al_titolare = ["atto_giudiziario"]

[destinatari]
titolare = "t@studio.it"
""")


def test_chiave_finita_per_sbaglio_dentro_destinatari_e_un_errore(tmp_path):
    """L'errore TOML facile da fare e impossibile da vedere: una chiave di
    primo livello scritta dopo [destinatari] finisce dentro [destinatari]."""
    with pytest.raises(DirectivesError, match="PRIMA di \\[destinatari\\]"):
        _load(tmp_path, """
[destinatari]
titolare = "t@studio.it"
tipi_documento = ["fattura", "altro"]
""")


def test_impronta_cambia_se_cambiano_le_direttive(tmp_path):
    a = _load(tmp_path, '[destinatari]\ntitolare = "t@studio.it"\n')
    b = _load(tmp_path, '[destinatari]\ntitolare = "t@studio.it"\n# commento\n')
    assert a.rules_digest != b.rules_digest
    assert a.stamp()["regole"] == a.rules_digest
