"""Gli esiti: append-only, e le correzioni accanto, non al posto."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone

import pytest

from pecdesk.outcomes import Outcome, OutcomeStore
from pecdesk.queue import move_to_worked
from pecfetch import permessi as perm


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


# -- i permessi sono quelli dichiarati, non quelli che capitano ---------------

@pytest.fixture
def umask_ostile():
    """Una umask che, da sola, darebbe il risultato sbagliato."""
    precedente = os.umask(0o077)
    yield
    os.umask(precedente)


def _modo(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_esiti_scritti_col_modello_dichiarato(tmp_path, umask_ostile):
    """`esiti/` è condivisa: pecfetch la crea, pecdesk ci scrive, e i due devono
    usare lo stesso modello — 2770 con setgid sulle cartelle, 0640 sui file —
    altrimenti chi sfoglia l'albero dalla rete vede metà tabella."""
    permessi = perm.Permessi()
    store = OutcomeStore(tmp_path / "esiti", permissions=permessi)
    store.append(_outcome("m1"))
    store.append_correction("m1", "tipo_documento", "fattura", "sollecito_pagamento")

    assert _modo(store.root) == permessi.shared_dir_mode == 0o2770
    assert _modo(store.root / "correzioni") == permessi.shared_dir_mode
    assert _modo(store.path_for()) == permessi.file_mode == 0o640
    assert _modo(store.corrections_path()) == permessi.file_mode


def test_un_file_gia_esistente_coi_modi_sbagliati_viene_corretto(tmp_path,
                                                                 umask_ostile):
    """Il file di ieri l'ha scritto una versione senza modello dei permessi."""
    store = OutcomeStore(tmp_path / "esiti")
    path = store.path_for()
    path.touch()
    os.chmod(path, 0o600)

    store.append(_outcome("m1"))
    assert _modo(path) == perm.Permessi().file_mode


def test_lavorati_esce_col_modo_condiviso(tmp_path, umask_ostile):
    """La cartella del giorno in `lavorati/` la crea pecdesk, ma la sfoglia chi
    guarda l'albero: stesso modello di `coda/` ed `esiti/`."""
    coda = tmp_path / "dati" / "coda"
    (coda / "20260907_rossi_m1").mkdir(parents=True)
    permessi = perm.Permessi()

    target = move_to_worked(coda / "20260907_rossi_m1",
                            tmp_path / "dati" / "lavorati",
                            when=datetime(2026, 9, 7, tzinfo=timezone.utc),
                            permissions=permessi)
    assert _modo(target.parent) == permessi.shared_dir_mode
