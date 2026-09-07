"""Regole sui tipi di file ostili: cosa si apre, cosa si scrive, con che limiti."""

import factories as f
import pytest

from pecfetch.safety import (
    ArchiveLimits,
    UnsafeXML,
    archive_kind,
    classify_file,
    extension,
    is_active_name,
    is_extractable,
    read_archive,
    safe_xml_fromstring,
    sniff,
    suffixes,
)


# ---------------------------------------------------------------------------
# nomi e impronte
# ---------------------------------------------------------------------------

def test_suffissi_multipli():
    assert suffixes("Fattura_2026.pdf.exe") == ["pdf", "exe"]
    assert extension("Atto.PDF") == "pdf"
    assert suffixes("senza-estensione") == []


def test_estensione_attiva_in_qualunque_suffisso():
    # il travestimento più vecchio del mestiere
    assert is_active_name("Fattura_2026.pdf.exe")
    assert is_active_name("apri.js")
    assert is_active_name("installa.MSI")
    assert not is_active_name("avviso.pdf")
    assert not is_active_name("prospetto.xlsx")


def test_estensioni_attive_aggiuntive():
    assert not is_active_name("macro.xyz")
    assert is_active_name("macro.xyz", extra={"xyz"})
    assert is_active_name("macro.xyz", extra={".xyz"})


@pytest.mark.parametrize("data,atteso", [
    (b"%PDF-1.7\n", "pdf"),
    (b"MZ\x90\x00", "pe"),
    (b"\x7fELF\x02", "elf"),
    (b"#!/bin/sh\n", "script"),
    (b"PK\x03\x04", "zip"),
    (b"\x1f\x8b\x08", "gz"),
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"", ""),
    (b"testo qualunque", ""),
])
def test_impronte(data, atteso):
    assert sniff(data) == atteso


def test_eseguibile_travestito_da_pdf():
    """Vale il contenuto, non l'estensione."""
    verdict = classify_file("Fattura 2026.pdf", "application/pdf", f.EXE_BYTES)
    assert verdict.active is True
    assert verdict.suspicious is True
    assert verdict.storable is False
    assert "eseguibile" in verdict.reason


def test_office_con_macro_si_salva_ma_si_segnala():
    verdict = classify_file("bilancio.xlsm", "", b"PK\x03\x04")
    assert verdict.active is False and verdict.storable is True
    assert verdict.macro_enabled is True


def test_documento_normale_passa_liscio():
    verdict = classify_file("avviso.pdf", "application/pdf", b"%PDF-1.4 x")
    assert not verdict.active and not verdict.suspicious and not verdict.macro_enabled


def test_content_type_eseguibile():
    assert classify_file("x.dat", "application/x-msdownload", b"dati").active


def test_tipi_da_cui_si_ricava_testo():
    assert is_extractable("atto.pdf") and is_extractable("nota.txt")
    assert is_extractable("busta.p7m") and is_extractable("interno.zip")
    assert not is_extractable("libreria.dll") and not is_extractable("dati.bin")


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------

def test_xml_normale():
    root = safe_xml_fromstring(b'<?xml version="1.0"?><a><b>x</b></a>')
    assert root.find("b").text == "x"


def test_dtd_rifiutato():
    bomba = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
             b"<lolz>&lol;</lolz>")
    with pytest.raises(UnsafeXML):
        safe_xml_fromstring(bomba)


def test_entita_rifiutata_anche_senza_doctype():
    with pytest.raises(UnsafeXML):
        safe_xml_fromstring(b"<!ENTITY x 'y'><a/>")


def test_xml_troppo_grande():
    with pytest.raises(UnsafeXML):
        safe_xml_fromstring(b"<a/>" + b" " * 100, max_bytes=10)


# ---------------------------------------------------------------------------
# archivi
# ---------------------------------------------------------------------------

def _leggi(data, nome="allegato.zip", limits=None, depth=0):
    limits = limits or ArchiveLimits()
    return read_archive(data, nome, limits.budget(len(data)), depth)


def test_zip_normale():
    data = f.zip_bytes([("cartella/fattura.pdf", b"%PDF-1.4 x"),
                        ("nota.txt", b"testo")])
    members, report = _leggi(data)
    assert [m.name for m in members] == ["fattura.pdf", "nota.txt"]
    assert report.format == "zip" and report.entries == 2
    assert not report.stopped


def test_zip_slip_ridotto_al_nome():
    """Il percorso dichiarato resta un dato, non diventa mai un percorso."""
    data = f.zip_bytes([("../../etc/passwd", b"root:x:0:0"),
                        ("/assoluto.txt", b"x"),
                        ("..\\..\\windows\\system32\\a.dll", b"y")])
    members, _report = _leggi(data)
    nomi = [m.name for m in members]
    assert nomi == ["passwd", "assoluto.txt", "a.dll"]
    assert all("/" not in n and "\\" not in n and ".." not in n for n in nomi)
    # il percorso originale non si perde: resta come dato
    assert members[0].declared_path == "../../etc/passwd"


def test_bomba_fermata_prima_di_espandersi_tutta():
    data = f.zip_bomb(size=8 * 1024 * 1024)
    limits = ArchiveLimits(max_ratio=10, max_total_bytes=100 * 1024 * 1024)
    _members, report = _leggi(data, limits=limits)
    assert "rapporto di espansione" in report.stopped
    # la guardia è incrementale: non si espande tutto per poi accorgersene
    assert report.expanded_bytes < 8 * 1024 * 1024


def test_limite_byte_totali():
    data = f.zip_bytes([(f"f{i}.txt", b"x" * 5000) for i in range(20)])
    _members, report = _leggi(data, limits=ArchiveLimits(max_total_bytes=12_000,
                                                         max_ratio=10_000))
    assert "byte espansi" in report.stopped
    assert report.expanded_bytes <= 12_000 + 65536


def test_limite_numero_voci():
    data = f.zip_bytes([(f"f{i}.txt", b"x") for i in range(60)])
    members, report = _leggi(data, limits=ArchiveLimits(max_entries=10))
    assert len(members) == 10
    assert "10 voci" in report.stopped


def test_voce_troppo_grande_saltata_non_fatale():
    data = f.zip_bytes([("piccolo.txt", b"ok"), ("grosso.txt", b"x" * 200_000)])
    members, report = _leggi(data, limits=ArchiveLimits(max_member_bytes=1000,
                                                        max_ratio=100_000))
    assert members[0].data == b"ok"
    assert "oltre 1000 byte" in members[1].skipped
    assert not report.stopped        # una voce sola non ferma tutto


def test_annidamento_oltre_il_limite():
    data = f.zip_annidato(livelli=2)
    members, report = _leggi(data, limits=ArchiveLimits(max_depth=3), depth=3)
    assert members == []
    assert "annidamento" in report.stopped


def test_zip_cifrato_non_si_tenta():
    data = f.zip_encrypted()
    members, report = _leggi(data)
    assert report.encrypted is True
    assert members[0].data == b""
    assert "password" in members[0].skipped


def test_tar_scarta_le_voci_non_regolari():
    data = f.tar_bytes([("documento.txt", b"testo")],
                       con_symlink=True, con_device=True)
    members, _report = _leggi(data, "archivio.tar")
    regolari = [m for m in members if m.data]
    saltate = {m.name: m.skipped for m in members if not m.data}
    assert [m.name for m in regolari] == ["documento.txt"]
    assert "symlink" in saltate["collegamento"]
    assert "device" in saltate["dispositivo"]


def test_gz_singolo():
    import gzip

    data = gzip.compress(b"contenuto in chiaro")
    members, report = _leggi(data, "nota.txt.gz")
    assert report.format == "gz"
    assert members[0].name == "nota.txt"
    assert members[0].data == b"contenuto in chiaro"


def test_targz_diventa_un_tar():
    import gzip

    data = gzip.compress(f.tar_bytes([("dentro.txt", b"testo")]))
    members, _report = _leggi(data, "pacco.tar.gz")
    assert members[0].name == "pacco.tar"


def test_formati_non_aperti_per_scelta():
    """rar e 7z sono i formati che il malspam usa per evadere: non si aprono."""
    for nome, magic in (("archivio.7z", b"7z\xbc\xaf\x27\x1c"),
                        ("archivio.rar", b"Rar!\x1a\x07\x00")):
        assert archive_kind(nome, magic) == "opaque"
        _members, report = _leggi(magic, nome)
        assert report.error and "scelta" in report.error
    # riconosciuti anche se il nome mente
    assert archive_kind("fatture.zip", b"Rar!\x1a\x07\x00") == "opaque"


def test_immagini_disco_sono_tipo_attivo():
    """.iso e simili non arrivano nemmeno al ramo archivi: sono tipi attivi."""
    for nome in ("consegna.iso", "backup.vhd", "disco.img"):
        assert is_active_name(nome)
        assert classify_file(nome, "", b"qualcosa").active


def test_archivio_corrotto_non_solleva():
    _members, report = _leggi(b"PK\x03\x04 troncato qui", "rotto.zip")
    assert report.error


def test_budget_condiviso_fra_i_livelli():
    """I contatori valgono per tutto l'albero, non per livello."""
    limits = ArchiveLimits(max_entries=5)
    budget = limits.budget(1000)
    primo = f.zip_bytes([(f"a{i}.txt", b"x") for i in range(4)])
    secondo = f.zip_bytes([(f"b{i}.txt", b"x") for i in range(4)])
    read_archive(primo, "a.zip", budget, 0)
    members, report = read_archive(secondo, "b.zip", budget, 1)
    assert len(members) <= 1
    assert "5 voci" in (report.stopped or budget.stopped)
