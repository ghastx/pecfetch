"""Gli esiti: append-only, e le correzioni accanto, non al posto."""

from __future__ import annotations

import json

from pecdesk.outcomes import Outcome, OutcomeStore


def _outcome(msg_id="m1", **kwargs):
    base = dict(id=msg_id, processed_at="2026-09-07T07:15:00+02:00",
                account="rossi", client="ROSSI", sender="x@pec.it",
                subject="Fattura 12", doc_type="fattura", klass="inoltro",
                route="studio", recipient_alias="contabilita",
                recipient="contabilita@studio.it", route_origin="modello",
                reason="fattura di fornitore", confidence="alta")
    base.update(kwargs)
    return Outcome(**base)


def test_scrittura_e_rilettura_conservano_tutto(tmp_path):
    store = OutcomeStore(tmp_path / "esiti")
    original = _outcome(deadline={"presente": True, "data": "2026-10-03",
                                  "verificato": True},
                        suspicion_signals=["mittente_mai_visto"],
                        material={"corpo_caratteri": 120},
                        directives={"versione": "test-1", "regole": "sha256:ab"})
    store.append(original)
    back = store.read()
    assert len(back) == 1
    assert back[0].to_json() == original.to_json()


def test_append_non_riscrive_le_righe_precedenti(tmp_path):
    store = OutcomeStore(tmp_path / "esiti")
    store.append(_outcome("m1"))
    store.append(_outcome("m2", klass="attenzione"))
    path = store.path_for()
    righe = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(righe) == 2
    assert json.loads(righe[0])["id"] == "m1"


def test_una_riga_tagliata_non_rende_illeggibile_il_file(tmp_path):
    store = OutcomeStore(tmp_path / "esiti")
    store.append(_outcome("m1"))
    with store.path_for().open("a", encoding="utf-8") as fh:
        fh.write('{"id": "tagli')          # interruzione a metà riga
    store.append(_outcome("m2"))
    letti = [o.id for o in store.read()]
    assert letti == ["m1", "m2"]


def test_la_correzione_sta_accanto_alla_proposta(tmp_path):
    store = OutcomeStore(tmp_path / "esiti")
    store.append(_outcome("m1", doc_type="fattura"))
    store.append_correction("m1", "tipo_documento", "fattura",
                            "sollecito_pagamento", author="titolare",
                            note="era un sollecito")

    # la proposta originale non è stata toccata
    assert store.find("m1")[0].doc_type == "fattura"
    correzioni = store.read_corrections()
    assert correzioni[0]["proposto"] == "fattura"
    assert correzioni[0]["corretto"] == "sollecito_pagamento"
    assert correzioni[0]["autore"] == "titolare"


def test_gli_id_scritti_servono_alla_riconciliazione(tmp_path):
    store = OutcomeStore(tmp_path / "esiti")
    store.append(_outcome("m1"))
    store.append(_outcome("m2"))
    assert store.known_ids() == {"m1", "m2"}
