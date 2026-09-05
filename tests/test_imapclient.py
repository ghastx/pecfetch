"""Dettagli del client IMAP che si possono provare senza un server."""

from datetime import date

from pecfetch.imapclient import _parse_internaldate, imap_date, quote_mailbox


def test_data_per_search():
    assert imap_date(date(2026, 1, 5)) == "05-Jan-2026"
    assert imap_date(date(2026, 12, 31)) == "31-Dec-2026"


def test_quoting_cartelle():
    assert quote_mailbox("INBOX") == '"INBOX"'
    assert quote_mailbox("Posta inviata") == '"Posta inviata"'
    assert quote_mailbox('strana"cartella') == '"strana\\"cartella"'


def test_utf7_modificato():
    # RFC 3501: i nomi non ASCII vanno in UTF-7 modificato.
    assert quote_mailbox("Böse") == '"B&APY-se"'
    assert quote_mailbox("a&b") == '"a&-b"'


def test_internaldate():
    blob = b'1 (UID 3 RFC822.SIZE 100 INTERNALDATE "05-Sep-2026 10:22:31 +0200")'
    got = _parse_internaldate(blob)
    assert got is not None and got.hour == 8   # normalizzata a UTC


def test_internaldate_assente():
    assert _parse_internaldate(b"1 (UID 3)") is None


def test_examine_e_readonly_nella_firma():
    """Difesa contro regressioni: la casella si apre solo in sola lettura."""
    import inspect

    from pecfetch.imapclient import ImapReader

    sorgente = inspect.getsource(ImapReader)
    assert "readonly=True" in sorgente
    assert "BODY.PEEK[]" in sorgente
    # nessun comando che modifica la casella
    for vietato in ("'STORE'", '"STORE"', "expunge", "'COPY'", "'MOVE'"):
        assert vietato not in sorgente
