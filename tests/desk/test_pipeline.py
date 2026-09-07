"""Dalla coda all'esito: l'ordine degli effetti, e cosa succede quando cade."""

from __future__ import annotations

from desk.fabbrica import FakeClassifier, write_message

from pecdesk.model import Judgment, ModelRefused, ModelStop, ModelUnavailable
from pecdesk.outcomes import OutcomeStore
from pecdesk.pipeline import Runner
from pecdesk.queue import read_item
from pecdesk.state import ARCHIVED, State


def _runner(config, directives, classifier, state=None):
    state = state or State(config.state_path)
    store = OutcomeStore(config.outcomes_dir, config.timezone)
    return Runner(config, directives, state, store, classifier, history=None), state, store


def test_un_messaggio_esce_dalla_coda_e_lascia_un_esito(config, directives,
                                                        queue_dir, judgment):
    write_message(queue_dir, "m1", sender="fornitore@pec.it")
    runner, state, store = _runner(config, directives, FakeClassifier(judgment))

    summary = runner.run()

    assert summary.classified == 1
    assert list(queue_dir.iterdir()) == []                # tolto dalla coda
    lavorati = list((config.worked_dir).rglob("messaggio.json"))
    assert len(lavorati) == 1                             # ...e messo altrove
    esiti = store.read()
    assert esiti[0].id == "m1"
    assert esiti[0].recipient == "contabilita@studio.it"
    assert state.get("m1")["stato"] == ARCHIVED
    state.close()


def test_i_file_di_input_non_vengono_modificati(config, directives, queue_dir,
                                                judgment):
    folder = write_message(queue_dir, "m1", sender="fornitore@pec.it")
    prima = {p.name: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    runner, state, _ = _runner(config, directives, FakeClassifier(judgment))
    runner.run()
    state.close()

    spostata = next(config.worked_dir.rglob("messaggio.json")).parent
    dopo = {p.name: p.read_bytes() for p in spostata.rglob("*") if p.is_file()}
    assert dopo == prima


def test_due_esecuzioni_ravvicinate_non_producono_esiti_doppi(config, directives,
                                                              queue_dir, judgment):
    write_message(queue_dir, "m1", sender="fornitore@pec.it")
    classifier = FakeClassifier(judgment)
    runner, state, store = _runner(config, directives, classifier)

    runner.run()
    runner.run()

    assert len(classifier.calls) == 1
    assert len(store.read()) == 1
    state.close()


def test_interruzione_fra_esito_e_stato_non_riclassifica(config, directives,
                                                         queue_dir, judgment):
    """Il caso vero: l'esito è su disco, lo stato non lo sa ancora."""
    write_message(queue_dir, "m1", sender="fornitore@pec.it")
    classifier = FakeClassifier(judgment)
    runner, state, store = _runner(config, directives, classifier)

    # si simula il guasto: la riga di esito è scritta, lo stato è fermo a in_corso
    state.claim("m1", "rossi")
    store.append(runner.classify_one(read_item(next(queue_dir.iterdir()))))
    assert len(classifier.calls) == 1

    summary = runner.run()

    assert summary.reconciled == 1
    assert len(classifier.calls) == 1                  # nessuna seconda chiamata
    assert len(store.read()) == 1                      # nessun secondo esito
    assert list(queue_dir.iterdir()) == []             # ma il messaggio è uscito
    state.close()


def test_api_irraggiungibile_lascia_il_messaggio_in_coda(config, directives,
                                                         queue_dir):
    write_message(queue_dir, "m1", sender="fornitore@pec.it")
    runner, state, store = _runner(
        config, directives, FakeClassifier(raises=ModelUnavailable("rete assente")))

    summary = runner.run()

    assert summary.classified == 0
    assert summary.failed == 1
    assert len(list(queue_dir.iterdir())) == 1         # resta in coda
    assert store.read() == []                          # nessuna classificazione inventata
    assert summary.unworked[0].reason == "rete assente"
    state.close()


def test_guasto_sistemico_ferma_tutto_ma_non_inventa_niente(config, directives,
                                                            queue_dir):
    for i in range(5):
        write_message(queue_dir, f"m{i}", sender="fornitore@pec.it")
    runner, state, store = _runner(
        config, directives, FakeClassifier(raises=ModelStop("credito esaurito")))

    summary = runner.run()

    assert "credito esaurito" in summary.stopped
    assert store.read() == []
    assert len(list(queue_dir.iterdir())) == 5
    # ...e il riepilogo li elenca comunque tutti
    digest = runner.digest_for(summary)
    assert len(digest.unworked) == 5
    assert any("credito esaurito" in n for n in digest.notes)
    state.close()


def test_cinque_fallimenti_di_fila_sono_un_guasto_non_un_caso(config, directives,
                                                              queue_dir):
    for i in range(8):
        write_message(queue_dir, f"m{i}", sender="fornitore@pec.it")
    classifier = FakeClassifier(raises=ModelUnavailable("timeout"))
    runner, state, _ = _runner(config, directives, classifier)

    summary = runner.run()

    assert len(classifier.calls) == config.api.stop_after_failures
    assert "fallimenti consecutivi" in summary.stopped
    state.close()


def test_richiesta_non_valida_sospende_senza_ritentare(config, directives,
                                                       queue_dir):
    write_message(queue_dir, "m1", sender="fornitore@pec.it")
    classifier = FakeClassifier(raises=ModelRefused("risposta non interpretabile"))
    runner, state, _ = _runner(config, directives, classifier)

    runner.run()
    runner.run()

    assert len(classifier.calls) == 1                  # non si ritenta
    assert state.get("m1")["stato"] == "sospeso"
    assert len(list(queue_dir.iterdir())) == 1         # e resta in coda
    state.close()


def test_la_regola_deterministica_non_chiama_il_modello(config, directives,
                                                        queue_dir):
    write_message(queue_dir, "m1", msg_type="errore_consegna")
    classifier = FakeClassifier(Judgment())
    runner, state, store = _runner(config, directives, classifier)

    summary = runner.run()

    assert classifier.calls == []                      # nessuna spesa
    assert summary.classified == 1
    esito = store.read()[0]
    assert esito.recipient == "segreteria@studio.it"
    assert esito.model["id"] == "nessuno"
    state.close()


def test_il_limite_lascia_il_resto_in_coda(config, directives, queue_dir,
                                           judgment):
    for i in range(5):
        write_message(queue_dir, f"m{i}", sender="fornitore@pec.it")
    runner, state, store = _runner(config, directives, FakeClassifier(judgment))

    summary = runner.run(limit=2)

    assert summary.classified == 2
    assert len(list(queue_dir.iterdir())) == 3
    assert len(summary.unworked) == 3
    state.close()


def test_un_messaggio_illeggibile_non_ferma_la_notte(config, directives,
                                                     queue_dir, judgment):
    write_message(queue_dir, "buono", sender="fornitore@pec.it")
    rotto = queue_dir / "20260907_rossi_rotto"
    rotto.mkdir()
    (rotto / "messaggio.json").write_text("{non è json", encoding="utf-8")

    runner, state, store = _runner(config, directives, FakeClassifier(judgment))
    summary = runner.run()

    assert summary.classified == 1
    assert any("metadati non validi" in p for p in summary.problems)
    state.close()


def test_il_riepilogo_parte_una_volta_sola(config, directives, queue_dir,
                                           judgment):
    write_message(queue_dir, "m1", sender="fornitore@pec.it")
    runner, state, _ = _runner(config, directives, FakeClassifier(judgment))
    summary = runner.run()
    digest = runner.digest_for(summary)

    primo, _ = runner.send_digest(digest)
    secondo, motivo = runner.send_digest(digest)

    assert primo is True
    assert secondo is False
    assert "già" in motivo
    # invio disattivato in configurazione: resta la copia su file
    assert (config.digest.copy_dir / f"{digest.day:%Y-%m-%d}.txt").is_file()
    state.close()
