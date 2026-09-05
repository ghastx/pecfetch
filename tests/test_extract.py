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
