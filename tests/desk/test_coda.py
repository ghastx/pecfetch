"""La coda vista dal consumatore: indirizzi, ordine, uscita dalla coda."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk.fabbrica import write_message
from pecdesk.queue import QueueError, move_to_worked, read_item, scan


def test_indirizzi_letti_minuscoli(queue_dir: Path):
    path = write_message(queue_dir, "m1", sender="Mario.Rossi@PEC.IT")
    item = read_item(path)
    assert item.sender == "mario.rossi@pec.it"
    assert item.sender_domain == "pec.it"
    assert item.mailbox_address == item.mailbox_address.lower()
    assert all(r == r.lower() for r in item.recipients)


def test_ordine_cronologico_anche_col_cambio_di_ora(queue_dir: Path):
    """Nell'ora del ritorno all'ora solare l'ordine alfabetico si inverte."""
    write_message(queue_dir, "prima", date="2026-10-25T02:30:00+02:00")
    write_message(queue_dir, "dopo", date="2026-10-25T02:30:00+01:00")
    items, problemi = scan(queue_dir)
    assert problemi == []
    assert [i.id for i in items] == ["prima", "dopo"]


def test_data_illeggibile_non_fa_saltare_l_ordinamento(queue_dir: Path):
    write_message(queue_dir, "buona", date="2026-09-07T08:30:00+02:00")
    write_message(queue_dir, "rotta", date="non una data")
    items, _ = scan(queue_dir)
    assert {i.id for i in items} == {"buona", "rotta"}
    assert items[0].id == "buona"       # le date vere prima, il resto in coda


def test_spostamento_fuori_dalla_coda(queue_dir: Path, tmp_path: Path):
    path = write_message(queue_dir, "m1")
    lavorati = tmp_path / "dati" / "lavorati"
    target = move_to_worked(path, lavorati)
    assert target.exists() and not path.exists()
    assert target.parent.parent == lavorati


def test_coda_non_scrivibile_non_duplica_in_silenzio(queue_dir: Path, tmp_path: Path,
                                                    monkeypatch):
    """Il caso vero: coda/ montata in sola lettura dall'unit systemd.

    Il rename fallisce con EROFS, si ripiega su copia più `rmtree`, e `rmtree`
    non cancella niente. Se la cosa resta silenziosa, le cartelle si duplicano
    in lavorati/ a ogni esecuzione senza mai lasciare la coda.
    """
    from pecdesk import queue as coda

    path = write_message(queue_dir, "m1")
    lavorati = tmp_path / "dati" / "lavorati"

    def rename_vietato(src, dst):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(coda.os, "rename", rename_vietato)
    monkeypatch.setattr(coda.shutil, "rmtree",
                        lambda p, ignore_errors=False: None)

    with pytest.raises(QueueError, match="non rimosso dalla coda"):
        move_to_worked(path, lavorati)
    assert path.exists()                          # il messaggio è ancora in coda


# -- il numero di schema è un cancello, non un'etichetta ---------------------

def test_lo_schema_dichiarato_viene_letto(queue_dir: Path):
    """La versione corrente si legge senza storie."""
    from pecfetch.output import SCHEMA_MESSAGE

    path = write_message(queue_dir, "m1")
    item = read_item(path)
    assert item.record["schema"] == SCHEMA_MESSAGE


def test_una_versione_sconosciuta_ferma_quel_messaggio(queue_dir: Path):
    """Il campo esiste per questo: se lo si ignora, tanto vale toglierlo.

    Una versione più alta può aver cambiato il significato dei campi senza
    cambiarne i nomi — è successo da /1 a /2 con le date e gli indirizzi — e
    leggerla lo stesso produrrebbe un messaggio senza mittente e senza data
    classificato come se fosse a posto.
    """
    path = write_message(queue_dir, "m1", schema="pecfetch/messaggio/3")

    with pytest.raises(QueueError, match="pecfetch/messaggio/3"):
        read_item(path)

    items, problemi = scan(queue_dir)
    assert items == []                                # non si lavora
    assert path.exists()                              # ...e resta in coda
    assert len(problemi) == 1
    assert "resta in coda" in problemi[0]


def test_anche_la_versione_precedente_viene_rifiutata(queue_dir: Path):
    """/1 aveva gli stessi campi con un altro significato: date con l'offset del
    gestore, indirizzi verbatim. Stessa forma non vuol dire leggibile."""
    path = write_message(queue_dir, "m1", schema="pecfetch/messaggio/1")
    with pytest.raises(QueueError, match="pecfetch/messaggio/1"):
        read_item(path)


def test_uno_schema_assente_non_e_un_record_di_pecfetch(queue_dir: Path):
    path = write_message(queue_dir, "m1")
    meta = path / "messaggio.json"
    record = json.loads(meta.read_text(encoding="utf-8"))
    del record["schema"]
    meta.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(QueueError, match="assente"):
        read_item(path)


def test_un_messaggio_a_schema_ignoto_non_ferma_gli_altri(queue_dir: Path):
    write_message(queue_dir, "buono", date="2026-09-07T08:30:00+02:00")
    write_message(queue_dir, "ignoto", date="2026-09-07T09:30:00+02:00",
                  schema="pecfetch/messaggio/9")
    items, problemi = scan(queue_dir)
    assert [i.id for i in items] == ["buono"]
    assert len(problemi) == 1
