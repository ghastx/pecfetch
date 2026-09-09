"""Contratto di output: due livelli, atomicità, corpo in un file a sé."""

import json
from datetime import datetime, timezone

import factories as f
import pytest

from pecfetch.output import DIR_QUEUE, OutputWriter
from pecfetch.pec import parse_pec
from pecfetch.state import content_hash, message_key


@pytest.fixture
def writer(cfg, stack):
    return stack[2]


def _scrivi(writer, account, raw):
    pm = parse_pec(raw)
    msg_id = message_key(account.id, content_hash(raw))
    return pm, writer.write_message(
        pm, raw, account, msg_id, 1000, 7, "2026-09-05T10:30:00+00:00"
    )


def test_struttura_creata(writer, cfg):
    for name in ("coda", "indice", "esiti", ".tmp-pecfetch"):
        assert (cfg.output_root / name).is_dir()
    assert (cfg.output_root / "CONTRATTO.md").is_file()
    assert (cfg.output_root / "esiti" / "LEGGIMI.md").is_file()


def test_contenuto_completo_di_un_messaggio(writer, cfg, account):
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("Atto n° 1/2026.pdf", b"%PDF-1.4 finto", "pdf")],
    ))
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir
    assert base.is_dir()

    # il corpo sta in un file di testo a sé, non dentro il JSON
    corpo = (base / "corpo.txt").read_text(encoding="utf-8")
    assert corpo.startswith("Si notifica")
    meta = json.loads((base / "messaggio.json").read_text(encoding="utf-8"))
    assert "Si notifica" not in json.dumps(meta)

    # sorgente originale e busta interna conservate
    assert (base / "busta.eml").read_bytes() == raw
    assert (base / "postacert.eml").is_file()
    assert (base / "daticert.xml").is_file()

    # allegato salvato con nome sanificato, originale conservato nei metadati
    stored = list((base / "allegati").glob("*.pdf"))
    assert len(stored) == 1
    assert "/" not in stored[0].name
    assert meta["allegati"][0]["nome"] == "Atto n° 1/2026.pdf"
    assert meta["allegati"][0]["sha256"]


def test_record_indice_compatto_e_completo(writer, cfg, account):
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("relazione.txt", "Testo della relazione".encode(), "octet-stream")],
    ))
    pm, res = _scrivi(writer, account, raw)
    rec = res.index_record

    assert rec["casella"]["etichetta"] == "Rossi S.r.l."
    assert rec["casella"]["cliente"] == "ROSSI"
    assert rec["tipo"] == "posta_certificata" and rec["certificato"] is True
    assert rec["data"]["certificata"] == "2026-09-05T10:22:31+02:00"
    assert rec["mittente"]["indirizzo"] == "ente@pec.comune.it"
    assert rec["mittente"]["dominio"] == "pec.comune.it"
    assert rec["destinatari"] == ["studio@pec.it"]
    assert rec["oggetto"] == "Avviso di accertamento n. 123/2026"
    assert rec["allegati"][0]["nome"] == "relazione.txt"
    assert rec["allegati"][0]["byte"] > 0
    assert rec["identificativo_pec"].startswith("opec")
    assert rec["contenuto"]["corpo"].endswith("corpo.txt")
    assert rec["imap"] == {"uidvalidity": 1000, "uid": 7}
    # un record d'indice deve restare piccolo: si legge tutto il giorno in blocco
    assert len(json.dumps(rec)) < 4000


def test_testo_degli_allegati_in_file_separati(writer, cfg, account):
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("nota.txt", "Importo dovuto: 1.234,00 euro".encode(), "octet-stream")],
    ))
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir
    testo = (base / "allegati" / "nota.txt.txt").read_text(encoding="utf-8")
    assert "1.234,00" in testo
    meta = json.loads((base / "messaggio.json").read_text(encoding="utf-8"))
    assert meta["allegati"][0]["testo"]["status"] == "ok"
    assert meta["allegati"][0]["testo"]["method"] == "plain"


def test_esito_estrazione_annotato_anche_quando_fallisce(writer, cfg, account):
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("misterioso.bin", b"\x00\x01\x02\x03", "octet-stream")],
    ))
    pm, res = _scrivi(writer, account, raw)
    # il messaggio esce comunque, con l'esito annotato
    assert (cfg.output_root / res.content_dir).is_dir()
    assert res.index_record["allegati"][0]["testo"] in ("unsupported", "empty", "failed")


def test_solo_html_diventa_testo(writer, cfg, account):
    raw = f.busta_trasporto(postacert=f.inner_message(
        body="", html="<html><body><p>Prima</p><p>Seconda</p></body></html>",
    ))
    pm, res = _scrivi(writer, account, raw)
    corpo = (cfg.output_root / res.content_dir / "corpo.txt").read_text(encoding="utf-8")
    assert "Prima" in corpo and "<p>" not in corpo
    assert res.index_record["contenuto"]["corpo_origine"] == "html"


def test_troncamento_dichiarato(cfg, account):
    from pecfetch.extract import ExtractorSettings

    writer = OutputWriter(cfg.output_root, ExtractorSettings(ocr=False),
                          body_max_chars=100)
    raw = f.busta_trasporto(postacert=f.inner_message(body="riga\n" * 500))
    pm, res = _scrivi(writer, account, raw)
    assert res.index_record["contenuto"]["corpo_troncato"] is True
    corpo = (cfg.output_root / res.content_dir / "corpo.txt").read_text(encoding="utf-8")
    assert "troncato" in corpo
    # il testo integrale resta comunque disponibile nella busta originale
    assert (cfg.output_root / res.content_dir / "busta.eml").stat().st_size > 1000


def test_allegato_oltre_soglia_censito_ma_non_salvato(cfg, account):
    from pecfetch.extract import ExtractorSettings

    writer = OutputWriter(cfg.output_root, ExtractorSettings(ocr=False),
                          attachment_store_max_bytes=10)
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("grosso.bin", b"x" * 5000, "octet-stream")],
    ))
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir
    assert not list((base / "allegati").iterdir())
    rec = res.index_record["allegati"][0]
    assert rec["nome"] == "grosso.bin" and rec["byte"] == 5000
    assert rec["testo"] == "skipped_too_large"


def test_scrittura_ripetuta_non_duplica(writer, cfg, account):
    raw = f.busta_trasporto()
    pm, primo = _scrivi(writer, account, raw)
    pm, secondo = _scrivi(writer, account, raw)
    assert secondo.already_present is True
    assert primo.content_dir == secondo.content_dir
    assert len(list((cfg.output_root / DIR_QUEUE).iterdir())) == 1


def test_niente_residui_nello_staging(writer, cfg, account):
    _scrivi(writer, account, f.busta_trasporto())
    assert not list((cfg.output_root / ".tmp-pecfetch").iterdir())


def test_indice_in_append_una_riga_per_messaggio(writer, cfg, account):
    quando = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        raw = f.busta_trasporto(postacert=f.inner_message(
            subject=f"Messaggio {i}", message_id=f"<m{i}@pec.it>"))
        _, res = _scrivi(writer, account, raw)
        rel = writer.append_index(res.index_record, quando)
    path = cfg.output_root / rel
    righe = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(righe) == 3
    # ogni riga è un record indipendente e valido da sola
    assert [json.loads(r)["oggetto"] for r in righe] == [
        "Messaggio 0", "Messaggio 1", "Messaggio 2"
    ]


def test_indice_giornaliero(writer, cfg):
    a = writer.index_path(datetime(2026, 9, 5, 23, 0, tzinfo=timezone.utc))
    b = writer.index_path(datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc))
    assert a.name == "2026-09-06.jsonl"   # 01:00 Roma del 6
    assert b.name == "2026-09-06.jsonl"


def test_cleanup_staging(writer, cfg):
    import os
    import time

    residuo = cfg.output_root / ".tmp-pecfetch" / "vecchio"
    residuo.mkdir(parents=True)
    (residuo / "a").write_text("x")
    vecchio = time.time() - 200_000
    os.utime(residuo, (vecchio, vecchio))
    assert writer.cleanup_staging() == 1
    assert not residuo.exists()


def test_nome_cartella_leggibile_e_stabile(writer, cfg, account):
    raw = f.busta_trasporto()
    _, res = _scrivi(writer, account, raw)
    nome = res.content_dir.split("/")[-1]
    assert nome.startswith("20260905-")
    assert "rossi" in nome
    assert "avviso-di-accertamento" in nome
    assert not any(c in nome for c in '<>:"/\\|?*')


# ---------------------------------------------------------------------------
# Allegati ostili: cosa finisce davvero su disco
# ---------------------------------------------------------------------------

def test_eseguibile_censito_ma_mai_scritto(writer, cfg, account):
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("Fattura_2026.pdf.exe", f.EXE_BYTES, "octet-stream")],
    ))
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir

    assert list((base / "allegati").iterdir()) == []
    meta = json.loads((base / "messaggio.json").read_text(encoding="utf-8"))
    allegato = meta["allegati"][0]
    assert allegato["salvato"] is False
    assert "tipo attivo" in allegato["motivo"]
    assert allegato["contenuto_attivo"] is True
    # censito per intero, e recuperabile dal sorgente originale conservato
    assert allegato["nome"] == "Fattura_2026.pdf.exe"
    assert allegato["byte"] == len(f.EXE_BYTES) and allegato["sha256"]
    assert b"Fattura_2026.pdf.exe" in (base / "postacert.eml").read_bytes()
    assert (base / "busta.eml").stat().st_size > 0


def test_eseguibile_travestito_da_pdf_non_scritto(writer, cfg, account):
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("Avviso.pdf", f.EXE_BYTES, "pdf")],
    ))
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir
    assert list((base / "allegati").iterdir()) == []
    meta = json.loads((base / "messaggio.json").read_text(encoding="utf-8"))
    assert meta["allegati"][0]["contenuto_sospetto"] is True
    assert meta["allegati"][0]["salvato"] is False


def test_office_con_macro_salvato_ma_segnalato(writer, cfg, account):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml",
                    '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
                    "<w:p><w:r><w:t>Con macro</w:t></w:r></w:p>"
                    "</w:body></w:document>")
    raw = f.busta_trasporto(postacert=f.inner_message(
        attachments=[("modulo.docm", buf.getvalue(), "octet-stream")],
    ))
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir
    assert (base / "allegati" / "modulo.docm").is_file()
    meta = json.loads((base / "messaggio.json").read_text(encoding="utf-8"))
    assert meta["allegati"][0]["contenuto_attivo"] is True
    assert "Con macro" in (base / "allegati" / "modulo.docm.txt").read_text()


def test_voci_di_archivio_con_nomi_generati(writer, cfg, account):
    contenuto = f.zip_bytes([
        ("fatture/Fattura 12.txt", "Imponibile 1.234,00 euro".encode()),
        ("../../etc/passwd", b"root:x:0:0"),
        ("Fattura_2026.pdf.exe", f.EXE_BYTES),
    ])
    raw = f.busta_con_archivio("Fatture.zip", contenuto)
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir
    member_dir = base / "allegati" / "Fatture.zip.d"

    scritti = sorted(p.name for p in member_dir.iterdir())
    assert scritti == ["001_Fattura 12.txt", "001_Fattura 12.txt.txt"]
    assert "1.234,00" in (member_dir / "001_Fattura 12.txt.txt").read_text()

    # nulla fuori dalla cartella del messaggio
    assert not (cfg.output_root / "etc").exists()
    for percorso in base.rglob("*"):
        assert base in percorso.parents or percorso.parent == base

    meta = json.loads((base / "messaggio.json").read_text(encoding="utf-8"))
    archivio = meta["allegati"][0]["archivio"]
    assert archivio["formato"] == "zip" and archivio["voci"] == 3
    per_nome = {m["nome"]: m for m in archivio["membri"]}
    assert per_nome["Fattura 12.txt"]["percorso_dichiarato"] == "fatture/Fattura 12.txt"
    assert per_nome["passwd"]["percorso_dichiarato"] == "../../etc/passwd"
    assert per_nome["passwd"]["salvato"] is False
    assert per_nome["Fattura_2026.pdf.exe"]["salvato"] is False


def test_archivio_annidato_scrive_solo_le_foglie(writer, cfg, account):
    interno = f.zip_bytes([("nota.txt", b"contenuto profondo")])
    esterno = f.zip_bytes([("interno.zip", interno)])
    raw = f.busta_con_archivio("pacco.zip", esterno)
    pm, res = _scrivi(writer, account, raw)
    member_dir = cfg.output_root / res.content_dir / "allegati" / "pacco.zip.d"
    scritti = sorted(p.name for p in member_dir.iterdir())
    assert scritti == ["001_nota.txt", "001_nota.txt.txt"]
    assert "contenuto profondo" in (member_dir / "001_nota.txt.txt").read_text()

    meta = json.loads((cfg.output_root / res.content_dir / "messaggio.json")
                      .read_text(encoding="utf-8"))
    membri = meta["allegati"][0]["archivio"]["membri"]
    assert membri[0]["percorso_dichiarato"] == "interno.zip/nota.txt"


def test_bomba_annotata_e_messaggio_comunque_scritto(cfg, account):
    from pecfetch.extract import ExtractorSettings
    from pecfetch.safety import ArchiveLimits

    writer = OutputWriter(cfg.output_root,
                          ExtractorSettings(ocr=False,
                                            archive=ArchiveLimits(max_ratio=10)))
    raw = f.busta_con_archivio("fatture.zip", f.zip_bomb(size=4 * 1024 * 1024))
    pm, res = _scrivi(writer, account, raw)
    base = cfg.output_root / res.content_dir
    assert (base / "corpo.txt").is_file()
    assert res.index_record["allegati"][0]["testo"] == "archive_limit"
    meta = json.loads((base / "messaggio.json").read_text(encoding="utf-8"))
    assert "rapporto" in meta["allegati"][0]["archivio"]["limite_superato"]


def test_indice_riporta_il_conteggio_delle_voci(writer, cfg, account):
    raw = f.busta_con_archivio()
    pm, res = _scrivi(writer, account, raw)
    allegato = res.index_record["allegati"][0]
    assert allegato["voci"] == 2
    assert allegato["nome"] == "Fatture.zip"


def test_record_dichiara_lo_schema_aggiornato(cfg, stack, account):
    """La forma non cambia, il significato dei campi sì: chi legge deve saperlo."""
    import json

    _state, _archive, writer = stack
    raw = f.busta_trasporto()
    result = writer.write_message(parse_pec(raw), raw, account, "id1", 1, 1, "x")
    assert result.index_record["schema"] == "pecfetch/indice/2"
    meta = json.loads(
        (cfg.output_root / result.content_dir / "messaggio.json").read_text("utf-8"))
    assert meta["schema"] == "pecfetch/messaggio/2"


def test_contratto_dichiara_fuso_permessi_e_indirizzi(cfg, stack):
    testo = (cfg.output_root / "CONTRATTO.md").read_text(encoding="utf-8")
    assert "Europe/Rome" in testo
    assert "0750" in testo and "2770" in testo
    assert "minuscolo" in testo


def test_contratto_riscritto_se_la_convenzione_cambia(cfg, stack, tmp_path):
    from pecfetch import permessi as perm
    from pecfetch.extract import ExtractorSettings
    from pecfetch.output import OutputWriter

    contratto = cfg.output_root / "CONTRATTO.md"
    assert "Europe/Rome" in contratto.read_text(encoding="utf-8")
    OutputWriter(cfg.output_root, ExtractorSettings(enabled=False),
                 timezone="UTC", permissions=perm.Permessi())
    assert "UTC" in contratto.read_text(encoding="utf-8")


def test_indirizzi_del_record_sono_minuscoli(cfg, stack, account):
    _state, _archive, writer = stack
    inner = f.inner_message(sender="Mario.Rossi@PEC.IT", to="LASERMARCSRL@PEC.IT")
    raw = f.busta_trasporto(postacert=inner, to="LASERMARCSRL@PEC.IT")
    record = writer.write_message(parse_pec(raw), raw, account, "id2", 1, 1,
                                  "x").index_record
    assert record["mittente"]["indirizzo"] == "mario.rossi@pec.it"
    assert record["destinatari"] == ["lasermarcsrl@pec.it"]
