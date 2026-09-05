"""Nomi compatibili con SMB/Windows, nome originale sempre conservato altrove."""

from pecfetch.naming import safe_filename, slugify, unique_filename


def test_caratteri_vietati():
    assert safe_filename('fatt/ura: n°1 <2026>.PDF') == "fatt_ura_ n°1 _2026_.PDF"


def test_nomi_riservati_dos():
    assert safe_filename("CON.pdf") == "_CON.pdf"
    assert safe_filename("lpt1") == "_lpt1"


def test_punto_e_spazio_finali():
    assert safe_filename("documento. ") == "documento"


def test_separatori_neutralizzati_senza_perdere_il_nome():
    # "Fattura 1/2026.pdf" non deve diventare "2026.pdf".
    assert safe_filename("Fattura 1/2026.pdf") == "Fattura 1_2026.pdf"
    for pericoloso in ("../../etc/passwd", "C:\\Windows\\note.txt", "..", "."):
        got = safe_filename(pericoloso)
        assert "/" not in got and "\\" not in got
        assert got not in ("..", ".")


def test_nome_vuoto():
    assert safe_filename("") == "allegato"
    assert safe_filename("   ") == "allegato"


def test_accenti_conservati():
    assert safe_filename("Perché sì.pdf") == "Perché sì.pdf"


def test_lunghezza_limitata_estensione_conservata():
    name = safe_filename("a" * 400 + ".pdf")
    assert len(name) <= 96 and name.endswith(".pdf")


def test_univocita_case_insensitive():
    taken: set[str] = set()
    assert unique_filename("Atto.pdf", taken) == "Atto.pdf"
    assert unique_filename("atto.pdf", taken) == "atto-1.pdf"
    assert unique_filename("ATTO.pdf", taken) == "ATTO-2.pdf"


def test_slug():
    assert slugify("Rossi S.r.l. — PEC") == "rossi-s.r.l.-pec"
    assert slugify("", fallback="x") == "x"
