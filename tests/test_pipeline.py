"""Comportamento di un'esecuzione, senza alcun server IMAP reale."""

import json
from datetime import datetime, timedelta, timezone

import factories as f
from conftest import FakeMailbox

from pecfetch.pipeline import MODE_BACKFILL, MODE_INIT, MODE_RUN
from pecfetch.state import STATUS_DONE, STATUS_SKIPPED


def _oggi() -> datetime:
    return datetime.now(timezone.utc)


def _coda(cfg):
    return sorted(p.name for p in (cfg.output_root / "coda").iterdir())


def _righe_indice(cfg):
    righe = []
    for path in sorted((cfg.output_root / "indice").glob("*.jsonl")):
        righe += [json.loads(r) for r in path.read_text(encoding="utf-8").splitlines() if r]
    return righe


# ---------------------------------------------------------------------------
# scaricamento
# ---------------------------------------------------------------------------

def test_run_scrive_messaggi_e_indice(cfg, stack, make_pipeline, account):
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    box.add(f.busta_anomalia(), _oggi())
    pipeline = make_pipeline({"rossi": box})

    summary = pipeline.run(MODE_RUN)
    assert summary.accounts_err == 0
    assert summary.written == 2
    assert len(_coda(cfg)) == 2

    righe = _righe_indice(cfg)
    assert len(righe) == 2
    assert {r["tipo"] for r in righe} == {"posta_certificata", "busta_anomalia"}
    assert {r["certificato"] for r in righe} == {True, False}
    # ogni record punta al proprio contenuto
    for record in righe:
        assert (cfg.output_root / record["contenuto"]["corpo"]).is_file()
        assert (cfg.output_root / record["contenuto"]["busta"]).is_file()


def test_due_esecuzioni_consecutive_non_duplicano(cfg, stack, make_pipeline):
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    pipeline = make_pipeline({"rossi": box})

    assert pipeline.run(MODE_RUN).written == 1
    assert pipeline.run(MODE_RUN).written == 0
    assert len(_coda(cfg)) == 1
    assert len(_righe_indice(cfg)) == 1


def test_ricevute_positive_non_finiscono_in_output(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox()
    box.add(f.ricevuta("accettazione"), _oggi())
    box.add(f.ricevuta("avvenuta-consegna"), _oggi())
    box.add(f.ricevuta("presa-in-carico"), _oggi())
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)

    assert summary.written == 0
    assert summary.receipts == 3
    assert _coda(cfg) == []
    assert _righe_indice(cfg) == []
    # registrate, non buttate via
    assert state.stats()["receipts"] == 3


def test_ricevute_negative_escono_collegate_all_originale(cfg, stack, make_pipeline):
    box = FakeMailbox()
    box.add(f.ricevuta("errore-consegna", errore="no-dest",
                       errore_esteso="casella inesistente"), _oggi())
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)

    assert summary.written == 1
    record = _righe_indice(cfg)[0]
    assert record["tipo"] == "errore_consegna"
    assert record["ricevuta"]["classe"] == "negativa"
    assert record["ricevuta"]["riferimento_message_id"] == "<originale-studio@pec.it>"
    assert "inesistente" in record["ricevuta"]["errore"]


def test_ordine_di_scrittura_stato_dopo_i_file(cfg, stack, make_pipeline, monkeypatch):
    """Se la scrittura su disco fallisce, il cursore NON deve avanzare."""
    state, _archive, writer = stack
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())

    def esplodi(*args, **kwargs):
        raise OSError("disco pieno")

    monkeypatch.setattr(writer, "write_message", esplodi)
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)

    assert summary.accounts_err == 1
    assert state.get_cursor("rossi", "INBOX").last_uid == 0
    assert _coda(cfg) == []

    # tolto l'ostacolo, il messaggio viene ripreso
    monkeypatch.undo()
    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 1


def test_interruzione_a_meta_non_lascia_file_parziali(cfg, stack, make_pipeline,
                                                      monkeypatch):
    state, _archive, writer = stack
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    originale = writer.append_index

    def esplodi(*args, **kwargs):
        raise OSError("indice non scrivibile")

    monkeypatch.setattr(writer, "append_index", esplodi)
    make_pipeline({"rossi": box}).run(MODE_RUN)

    # i file del messaggio ci sono (fase completata), ma il cursore è fermo
    assert state.get_cursor("rossi", "INBOX").last_uid == 0
    riga = state.get_message("rossi", 1000, 1)
    assert riga["files_done"] == 1 and riga["index_done"] == 0
    assert not list((cfg.output_root / ".tmp-pecfetch").iterdir())

    monkeypatch.setattr(writer, "append_index", originale)
    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 1
    assert len(_coda(cfg)) == 1          # nessun duplicato
    assert len(_righe_indice(cfg)) == 1


# ---------------------------------------------------------------------------
# UID, UIDVALIDITY, deduplica
# ---------------------------------------------------------------------------

def test_uid_avanza_solo_in_avanti(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    make_pipeline({"rossi": box}).run(MODE_RUN)
    assert state.get_cursor("rossi", "INBOX").last_uid == 1

    box.add(f.messaggio_semplice(), _oggi())
    make_pipeline({"rossi": box}).run(MODE_RUN)
    assert state.get_cursor("rossi", "INBOX").last_uid == 2


def test_uidvalidity_cambiata_risincronizza_senza_duplicare(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox(uidvalidity=1000)
    box.add(f.busta_trasporto(), _oggi())
    box.add(f.messaggio_semplice(), _oggi())
    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 2

    # il gestore ricrea la casella: stessi messaggi, UID da capo
    box.renumber(new_uidvalidity=2000, start=1)
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)

    assert summary.accounts_err == 0
    assert summary.written == 0
    assert summary.duplicates == 2       # riconosciuti dal contenuto
    assert len(_coda(cfg)) == 2
    assert len(_righe_indice(cfg)) == 2
    assert state.get_cursor("rossi", "INBOX").uidvalidity == 2000


def test_deduplica_per_contenuto_su_uid_diverso(cfg, stack, make_pipeline):
    box = FakeMailbox()
    raw = f.busta_trasporto()
    box.add(raw, _oggi())
    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 1
    box.add(raw, _oggi())                # il gestore riconsegna lo stesso messaggio
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)
    assert summary.written == 0 and summary.duplicates == 1
    assert len(_coda(cfg)) == 1


def test_stessa_pec_su_due_caselle_non_e_un_duplicato(cfg, stack, make_pipeline):
    from pecfetch.config import replace

    seconda = replace(cfg.accounts[0], id="bianchi", address="bianchi@pec.it",
                      label="Bianchi S.p.A.", client_id="BIANCHI")
    raw = f.busta_trasporto()
    box_a, box_b = FakeMailbox(), FakeMailbox()
    box_a.add(raw, _oggi())
    box_b.add(raw, _oggi())

    pipeline = make_pipeline({"rossi": box_a, "bianchi": box_b})
    summary = pipeline.run(MODE_RUN, accounts=[cfg.accounts[0], seconda])
    assert summary.written == 2
    assert {r["casella"]["id"] for r in _righe_indice(cfg)} == {"rossi", "bianchi"}


# ---------------------------------------------------------------------------
# primo avvio
# ---------------------------------------------------------------------------

def test_primo_avvio_prende_solo_i_recenti(cfg, stack, make_pipeline):
    box = FakeMailbox()
    box.add(f.messaggio_semplice(subject="vecchio"), _oggi() - timedelta(days=800))
    box.add(f.messaggio_semplice(subject="recente"), _oggi())
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)
    assert summary.written == 1
    assert _righe_indice(cfg)[0]["oggetto"] == "recente"


def test_init_fissa_la_posizione_senza_scaricare(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox()
    for i in range(5):
        box.add(f.messaggio_semplice(subject=f"m{i}"), _oggi())

    make_pipeline({"rossi": box}).run(MODE_INIT)
    assert _coda(cfg) == []
    assert state.get_cursor("rossi", "INBOX").last_uid == 5
    assert box.fetched == []

    # da qui in poi solo la posta nuova
    box.add(f.messaggio_semplice(subject="nuovo"), _oggi())
    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 1
    assert _righe_indice(cfg)[0]["oggetto"] == "nuovo"


def test_init_con_lookback(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox()
    box.add(f.messaggio_semplice(subject="vecchio"), _oggi() - timedelta(days=30))
    box.add(f.messaggio_semplice(subject="ieri"), _oggi() - timedelta(days=1))
    make_pipeline({"rossi": box}).run(MODE_INIT, lookback_days=3)
    assert state.get_cursor("rossi", "INBOX").last_uid == 1
    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 1


def test_backfill_esplicito(cfg, stack, make_pipeline):
    box = FakeMailbox()
    box.add(f.messaggio_semplice(subject="storico"), _oggi() - timedelta(days=300))
    box.add(f.messaggio_semplice(subject="recente"), _oggi())

    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 1
    summary = make_pipeline({"rossi": box}).run(
        MODE_BACKFILL, since=(_oggi() - timedelta(days=400)).date()
    )
    assert summary.written == 1
    assert {r["oggetto"] for r in _righe_indice(cfg)} == {"storico", "recente"}


# ---------------------------------------------------------------------------
# robustezza
# ---------------------------------------------------------------------------

def test_casella_rotta_non_ferma_le_altre(cfg, stack, make_pipeline):
    from pecfetch.config import replace

    rotta = replace(cfg.accounts[0], id="rotta", address="rotta@pec.it")
    box_ok = FakeMailbox()
    box_ok.add(f.busta_trasporto(), _oggi())
    box_ko = FakeMailbox()
    box_ko.fail_on_connect = "login fallito per rotta@pec.it: AUTHENTICATIONFAILED"

    pipeline = make_pipeline({"rossi": box_ok, "rotta": box_ko})
    summary = pipeline.run(MODE_RUN, accounts=[rotta, cfg.accounts[0]])

    assert summary.accounts_err == 1 and summary.accounts_ok == 1
    assert summary.written == 1
    assert summary.errors[0][0] == "rotta"
    assert "AUTHENTICATIONFAILED" in summary.errors[0][1]


def test_errore_su_examine_registrato(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox()
    box.fail_on_examine = "EXAMINE di 'INBOX' fallito"
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)
    assert summary.accounts_err == 1
    riga = [r for r in state.mailbox_rows() if r["account"] == "rossi"][0]
    assert riga["last_error"]


def test_messaggio_troppo_grande_censito_e_saltato(cfg, stack, make_pipeline, monkeypatch):
    from pecfetch.config import replace

    piccolo = replace(cfg, max_message_bytes=100)
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    box.add(f.messaggio_semplice(), _oggi())
    pipeline = make_pipeline({"rossi": box})
    pipeline.cfg = piccolo
    summary = pipeline.run(MODE_RUN)
    assert summary.results[0].skipped == 2
    assert summary.written == 0
    # il cursore avanza comunque: altrimenti la casella resterebbe bloccata
    assert stack[0].get_cursor("rossi", "INBOX").last_uid == 2


def test_limite_per_esecuzione(cfg, stack, make_pipeline):
    box = FakeMailbox()
    for i in range(5):
        box.add(f.messaggio_semplice(subject=f"m{i}"), _oggi())
    summary = make_pipeline({"rossi": box}).run(MODE_RUN, limit=2)
    assert summary.written == 2
    assert summary.remaining == 3
    assert make_pipeline({"rossi": box}).run(MODE_RUN, limit=2).written == 2


def test_dry_run_non_scrive_nulla(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    summary = make_pipeline({"rossi": box}).run(MODE_RUN, dry_run=True)
    assert summary.written == 1          # "scaricherei"
    assert _coda(cfg) == []
    assert state.get_cursor("rossi", "INBOX").last_uid == 0


def test_casella_disabilitata_saltata(cfg, stack, make_pipeline):
    from pecfetch.config import replace

    spenta = replace(cfg.accounts[0], enabled=False)
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    summary = make_pipeline({"rossi": box}).run(MODE_RUN, accounts=[spenta])
    assert summary.results == []
    assert _coda(cfg) == []


def test_archivio_popolato_e_interrogabile(cfg, stack, make_pipeline):
    archive = stack[1]
    box = FakeMailbox()
    box.add(f.busta_trasporto(postacert=f.inner_message(
        subject="Cartella esattoriale 2026",
        body="Si comunica l'iscrizione a ruolo per omesso versamento.",
    )), _oggi())
    make_pipeline({"rossi": box}).run(MODE_RUN)

    assert archive.stats()["totale"] == 1
    assert archive.search(query="esattoriale")
    assert archive.search(query="ruolo")[0].client == "ROSSI"
    assert archive.search(sender="pec.comune.it")
    assert archive.search(client="ROSSI")
    assert archive.search(query="parolachenonesiste") == []


def test_archivio_rotto_non_perde_il_messaggio(cfg, stack, make_pipeline, monkeypatch):
    archive = stack[1]
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())

    def esplodi(*args, **kwargs):
        raise RuntimeError("archivio non raggiungibile")

    monkeypatch.setattr(archive, "add", esplodi)
    summary = make_pipeline({"rossi": box}).run(MODE_RUN)
    assert summary.written == 1
    assert len(_coda(cfg)) == 1
    assert stack[0].get_cursor("rossi", "INBOX").last_uid == 1


def test_il_consumatore_puo_svuotare_la_coda(cfg, stack, make_pipeline):
    """La verità su cosa è stato scaricato sta nello stato, non nei file."""
    import shutil

    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    make_pipeline({"rossi": box}).run(MODE_RUN)

    for entry in (cfg.output_root / "coda").iterdir():
        shutil.rmtree(entry)

    assert make_pipeline({"rossi": box}).run(MODE_RUN).written == 0
    assert _coda(cfg) == []


def test_sequenza_completa_realistica(cfg, stack, make_pipeline):
    state = stack[0]
    box = FakeMailbox()
    box.add(f.busta_trasporto(), _oggi())
    box.add(f.ricevuta("accettazione"), _oggi())
    box.add(f.ricevuta("avvenuta-consegna"), _oggi())
    box.add(f.ricevuta("errore-consegna", errore_esteso="dominio inesistente"), _oggi())
    box.add(f.busta_anomalia(), _oggi())
    box.add(f.messaggio_semplice(), _oggi())

    summary = make_pipeline({"rossi": box}).run(MODE_RUN)
    assert summary.written == 4          # busta, ricevuta negativa, anomalia, generico
    assert summary.receipts == 2
    tipi = sorted(r["tipo"] for r in _righe_indice(cfg))
    assert tipi == ["busta_anomalia", "errore_consegna", "generico", "posta_certificata"]
    assert state.get_cursor("rossi", "INBOX").last_uid == 6
    stati = {r["uid"]: r["status"] for r in state.db.execute("SELECT uid, status FROM messages")}
    assert stati == {1: STATUS_DONE, 2: STATUS_SKIPPED, 3: STATUS_SKIPPED,
                     4: STATUS_DONE, 5: STATUS_DONE, 6: STATUS_DONE}


def test_allegato_ostile_non_fa_mancare_il_messaggio(cfg, stack, make_pipeline):
    """La regola che conta: annotato, non assente."""
    box = FakeMailbox()
    box.add(f.busta_con_archivio("Fatture.zip", f.zip_bytes([
        ("Fattura 12.txt", "Imponibile 1.234,00 euro".encode()),
        ("Fattura_2026.pdf.exe", f.EXE_BYTES),
    ])), _oggi())
    box.add(f.busta_con_archivio("bomba.zip", f.zip_bomb(size=4 * 1024 * 1024)),
            _oggi())
    box.add(f.busta_con_archivio("protetto.zip", f.zip_encrypted()), _oggi())
    box.add(f.busta_trasporto(postacert=f.inner_message(
        subject="Solo un eseguibile",
        attachments=[("aggiornamento.exe", f.EXE_BYTES, "octet-stream")])), _oggi())

    summary = make_pipeline({"rossi": box}).run(MODE_RUN)

    assert summary.accounts_err == 0
    assert summary.written == 4          # nessun messaggio perso
    assert len(_coda(cfg)) == 4
    righe = _righe_indice(cfg)
    assert len(righe) == 4

    stati = {r["allegati"][0]["nome"]: r["allegati"][0]["testo"] for r in righe}
    assert stati["bomba.zip"] == "archive_limit"
    assert stati["protetto.zip"] == "encrypted"
    assert stati["aggiornamento.exe"] == "blocked_type"

    # nessun eseguibile sciolto da nessuna parte sotto la radice
    for percorso in (cfg.output_root / "coda").rglob("*"):
        assert not percorso.name.lower().endswith((".exe", ".bat", ".js", ".vbs"))


def test_archivio_full_text_indicizza_le_voci(cfg, stack, make_pipeline):
    archive = stack[1]
    box = FakeMailbox()
    box.add(f.busta_con_archivio("Fatture.zip", f.zip_bytes([
        ("Cartella esattoriale.txt", "Iscrizione a ruolo per omesso versamento".encode()),
    ])), _oggi())
    make_pipeline({"rossi": box}).run(MODE_RUN)
    assert archive.search(query="esattoriale")
    assert archive.search(query="ruolo")
