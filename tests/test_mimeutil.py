"""Decodifica: deve degradare, mai far cadere il run."""

from pecfetch.mimeutil import (
    decode_bytes,
    decode_header_value,
    html_to_text,
    normalize_text,
    parse_addresses,
    parse_date,
)


def test_rfc2047_misto():
    raw = "=?utf-8?B?QXZ2aXNvIGRpIGFjY2VydGFtZW50bw==?= n. 1 =?iso-8859-1?Q?perch=E9?="
    assert decode_header_value(raw) == "Avviso di accertamento n. 1 perché"


def test_rfc2047_malformato_non_esplode():
    assert decode_header_value("=?utf-8?B?non-base64-valido?= coda")


def test_header_con_folding():
    assert decode_header_value("prima\r\n  seconda") == "prima seconda"


def test_charset_dichiarato_male_ma_utf8():
    # Caso italiano tipico: dichiarato latin-1, in realtà UTF-8.
    assert decode_bytes("perché".encode("utf-8"), "iso-8859-1") == "perché"


def test_charset_latin1_davvero_latin1():
    assert decode_bytes("perché".encode("latin-1"), "iso-8859-1") == "perché"


def test_charset_inesistente():
    assert decode_bytes("città".encode("utf-8"), "x-unknown") == "città"


def test_bytes_illeggibili_non_sollevano():
    assert decode_bytes(b"\xff\xfe\x00rotto", "utf-8")


def test_html_to_text_niente_tag():
    html = ("<html><head><style>p{color:red}</style></head><body>"
            "<p>Ciao<br>mondo</p><ul><li>uno</li><li>due</li></ul>"
            "<script>alert(1)</script></body></html>")
    text = html_to_text(html)
    assert "Ciao\nmondo" in text
    assert "- uno" in text and "- due" in text
    assert "color" not in text and "alert" not in text


def test_html_entita():
    assert "perché & altro" in html_to_text("<p>perch&eacute; &amp; altro</p>")


def test_indirizzi():
    got = parse_addresses('"Rossi, Mario" <m.rossi@pec.it>, altro@pec.it')
    assert [a["address"] for a in got] == ["m.rossi@pec.it", "altro@pec.it"]
    assert got[0]["name"] == "Rossi, Mario"
    assert got[0]["domain"] == "pec.it"


def test_indirizzi_spazzatura():
    assert parse_addresses("non-un-indirizzo") or True  # non deve sollevare


def test_data_non_parsabile():
    assert parse_date("un giorno qualsiasi") is None
    assert parse_date(None) is None


def test_normalize_text_paragrafi():
    assert normalize_text("a\r\n\r\n\r\n\r\nb   \n") == "a\n\nb"
