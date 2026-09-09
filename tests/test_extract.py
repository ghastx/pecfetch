"""Estrazione del testo: mai un'eccezione fuori, esito sempre dichiarato."""

import io
import shutil
import zipfile

import pytest

from pecfetch.extract import (
    STATUS_OK,
    ExtractorSettings,
    available_tools,
    extract_text,
)

S = ExtractorSettings(ocr=False)


def test_testo_semplice():
    res = extract_text("Importo: 1.234,00 €".encode(), "nota.txt", "text/plain", S)
    assert res.status == STATUS_OK and res.method == "plain"
    assert "1.234,00" in res.text


def test_charset_bugiardo_non_rompe():
    res = extract_text("perché".encode("latin-1"), "n.txt", "text/plain", S)
    assert res.status == STATUS_OK and "perch" in res.text


def test_html_diventa_testo():
    res = extract_text(b"<p>Prima</p><p>Seconda</p>", "a.html", "text/html", S)
    assert "Prima" in res.text and "<p>" not in res.text


def test_docx_senza_dipendenze():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml",
                    '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
                    '<w:p><w:r><w:t>Avviso di accertamento</w:t></w:r></w:p>'
                    '<w:p><w:r><w:t>Importo &amp; termini</w:t></w:r></w:p>'
                    "</w:body></w:document>")
    res = extract_text(buf.getvalue(), "atto.docx", "", S)
    assert res.method == "docx_xml"
    assert "Avviso di accertamento" in res.text
    assert "Importo & termini" in res.text


def test_xlsx_senza_dipendenze():
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml",
                    f'<sst xmlns="{ns}"><si><t>Imponibile</t></si></sst>')
        zf.writestr("xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{ns}"><sheetData>'
                    '<row><c t="s"><v>0</v></c><c><v>1234.5</v></c></row>'
                    "</sheetData></worksheet>")
    res = extract_text(buf.getvalue(), "prospetto.xlsx", "", S)
    assert "Imponibile" in res.text and "1234.5" in res.text


def test_zip_corrotto_non_solleva():
    res = extract_text(b"PK\x03\x04rotto", "x.docx", "", S)
    assert res.status == "failed" and res.error


def test_tipo_non_gestito_censito():
    res = extract_text(b"\x00\x01\x02", "x.bin", "application/octet-stream", S)
    assert res.status == "unsupported"
    assert res.as_dict()["status"] == "unsupported"


def test_oltre_soglia_non_estratto_ma_dichiarato():
    piccolo = ExtractorSettings(max_bytes=10, ocr=False)
    res = extract_text(b"x" * 100, "grosso.txt", "text/plain", piccolo)
    assert res.status == "skipped_too_large"


def test_troncamento_dichiarato():
    corto = ExtractorSettings(max_chars=20, ocr=False)
    res = extract_text(("riga\n" * 100).encode(), "a.txt", "text/plain", corto)
    assert res.truncated is True and len(res.text) == 20
    assert res.as_dict()["truncated"] is True


def test_estrazione_disabilitata():
    res = extract_text(b"testo", "a.txt", "text/plain", ExtractorSettings(enabled=False))
    assert res.status == "disabled"


def test_pdf_senza_strumenti_dichiara_tool_missing():
    res = extract_text(b"%PDF-1.4\n(finto)", "a.pdf", "application/pdf", S)
    # Con gli strumenti installati sarebbe 'empty'; senza, 'tool_missing'.
    assert res.status in ("tool_missing", "empty", "ok")
    assert res.as_dict()["method"]


def test_eml_allegato():
    raw = (b"Subject: Comunicazione\r\nFrom: a@b.it\r\n"
           b"Content-Type: text/plain\r\n\r\nCorpo della mail allegata.")
    res = extract_text(raw, "inoltro.eml", "message/rfc822", S)
    assert "Comunicazione" in res.text and "Corpo della mail" in res.text


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl assente")
def test_p7m_non_valido_dichiara_l_errore():
    res = extract_text(b"non e' una busta cades", "atto.pdf.p7m",
                       "application/pkcs7-mime", S)
    assert res.status == "failed" and res.method == "p7m"


def test_diagnostica_strumenti():
    tools = available_tools()
    assert set(tools) >= {"pdftotext", "tesseract", "openssl", "pypdf"}


# ---------------------------------------------------------------------------
# Allegati ostili
# ---------------------------------------------------------------------------

import factories as f  # noqa: E402

from pecfetch.extract import (  # noqa: E402
    STATUS_ARCHIVE_LIMIT,
    STATUS_BLOCKED,
    STATUS_ENCRYPTED,
    STATUS_OPAQUE_ARCHIVE,
)
from pecfetch.safety import ArchiveLimits  # noqa: E402


def test_eseguibile_non_viene_nemmeno_aperto():
    res = extract_text(f.EXE_BYTES, "Fattura.exe", "", S)
    assert res.status == STATUS_BLOCKED
    assert "estensione attiva" in res.error


def test_eseguibile_travestito_da_pdf():
    res = extract_text(f.EXE_BYTES, "Fattura 2026.pdf", "application/pdf", S)
    assert res.status == STATUS_BLOCKED
    assert "eseguibile" in res.error


def test_doppia_estensione():
    assert extract_text(f.EXE_BYTES, "Fattura.pdf.exe", "", S).status == STATUS_BLOCKED


def test_zip_estrae_solo_i_tipi_idonei():
    data = f.zip_bytes([
        ("fatture/Fattura 12.txt", "Imponibile 1.234,00 euro".encode()),
        ("Fattura_2026.pdf.exe", f.EXE_BYTES),
        ("dati.bin", b"\x00\x01\x02"),
    ])
    res = extract_text(data, "Fatture.zip", "application/zip", S)
    assert res.status == STATUS_OK
    assert "1.234,00" in res.text

    per_nome = {m.name: m for m in res.members}
    assert per_nome["Fattura 12.txt"].stored is True
    assert per_nome["Fattura_2026.pdf.exe"].stored is False
    assert per_nome["Fattura_2026.pdf.exe"].result.status == STATUS_BLOCKED
    assert per_nome["dati.bin"].stored is False
    # il percorso dichiarato resta un dato
    assert per_nome["Fattura 12.txt"].declared_path == "fatture/Fattura 12.txt"


def test_zip_slip_nel_dispatcher():
    data = f.zip_bytes([("../../etc/passwd", b"root:x:0:0")])
    res = extract_text(data, "posta.zip", "", S)
    assert [m.name for m in res.members] == ["passwd"]


def test_bomba_annotata_e_non_fatale():
    settings = ExtractorSettings(ocr=False,
                                 archive=ArchiveLimits(max_ratio=10))
    res = extract_text(f.zip_bomb(size=4 * 1024 * 1024), "fatture.zip", "", settings)
    assert res.status == STATUS_ARCHIVE_LIMIT
    assert "rapporto" in res.error
    assert res.archive.expanded_bytes < 4 * 1024 * 1024


def test_zip_cifrato():
    res = extract_text(f.zip_encrypted(), "documenti.zip", "", S)
    assert res.status == STATUS_ENCRYPTED
    assert res.archive.encrypted is True


def test_formato_non_aperto():
    res = extract_text(b"7z\xbc\xaf\x27\x1c dati", "fatture.7z", "", S)
    assert res.status == STATUS_OPAQUE_ARCHIVE
    assert "busta.eml" in res.error


def test_annidamento_limitato():
    settings = ExtractorSettings(ocr=False, max_depth=2,
                                 archive=ArchiveLimits(max_depth=2))
    res = extract_text(f.zip_annidato(livelli=4), "pacco.zip", "", settings)
    # non esplode e non arriva in fondo
    assert "testo in fondo" not in res.text


def test_zip_dentro_zip_arriva_al_testo():
    interno = f.zip_bytes([("nota.txt", b"contenuto profondo")])
    esterno = f.zip_bytes([("interno.zip", interno)])
    res = extract_text(esterno, "esterno.zip", "", S)
    assert "contenuto profondo" in res.text


def test_office_con_troppa_espansione():
    """Un office è uno ZIP: stessa bomba, stessa guardia."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml",
                    '<?xml version="1.0"?><w:document xmlns:w="x"><w:body><w:p>'
                    + "<w:r><w:t>x</w:t></w:r>" * 200000
                    + "</w:p></w:body></w:document>")
    settings = ExtractorSettings(ocr=False, archive=ArchiveLimits(max_ratio=5))
    res = extract_text(buf.getvalue(), "atto.docx", "", settings)
    assert res.status in ("failed", "ok", "empty")   # non solleva, comunque


def test_xlsx_con_bomba_di_entita():
    import io
    import zipfile

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml",
                    '<?xml version="1.0"?><!DOCTYPE s [<!ENTITY lol "AAAA">]>'
                    f'<sst xmlns="{ns}"><si><t>&lol;</t></si></sst>')
        zf.writestr("xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{ns}"><sheetData/></worksheet>')
    res = extract_text(buf.getvalue(), "prospetto.xlsx", "", S)
    assert res.status == "failed"
    assert res.error


def test_archivi_disabilitati():
    settings = ExtractorSettings(ocr=False, archive=ArchiveLimits(enabled=False))
    res = extract_text(f.zip_bytes([("a.txt", b"x")]), "a.zip", "", settings)
    assert res.status == "disabled"


def test_blocco_disattivabile():
    settings = ExtractorSettings(ocr=False, block_active_types=False)
    res = extract_text(b"#!/bin/sh\necho ciao", "script.sh", "", settings)
    assert res.status != STATUS_BLOCKED


def test_lingue_ocr_senza_l_intestazione(monkeypatch):
    """`--list-langs` stampa prima una riga di intestazione, e non è una lingua."""
    from pecfetch import extract

    uscita = b"List of available languages (3):\nosd\nita\neng\n"
    monkeypatch.setattr(extract, "_have", lambda tool: tool == "tesseract")
    monkeypatch.setattr(extract, "_optional_import", lambda mod: None)
    monkeypatch.setattr(extract, "_run", lambda cmd, timeout: (0, uscita, b""))
    assert extract.available_tools()["tesseract_langs"] == ["eng", "ita", "osd"]


def test_lingue_ocr_forme_particolari():
    from pecfetch.extract import _parse_langs

    testa = 'List of available languages in "/usr/share/tessdata/" (4):'
    assert _parse_langs(f"{testa}\nchi_sim\nscript/Latin\nita\nosd\n") == [
        "chi_sim", "ita", "osd", "script/Latin"]
    # senza intestazione (tesseract 3) non si perde la prima lingua
    assert _parse_langs("eng\nita\n") == ["eng", "ita"]
    assert _parse_langs("") == []


def test_lingue_ocr_comando_fallito(monkeypatch):
    """Con un codice di uscita diverso da zero non si stampa quello che capita."""
    from pecfetch import extract

    monkeypatch.setattr(extract, "_have", lambda tool: tool == "tesseract")
    monkeypatch.setattr(extract, "_optional_import", lambda mod: None)
    monkeypatch.setattr(extract, "_run",
                        lambda cmd, timeout: (1, b"Error opening data file\n", b""))
    assert extract.available_tools()["tesseract_langs"] == []
