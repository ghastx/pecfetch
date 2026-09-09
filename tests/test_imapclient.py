"""Dettagli del client IMAP che si possono provare senza un server."""

import contextlib
import os
import time
from datetime import date, datetime, timezone

import pytest

from pecfetch.imapclient import _parse_internaldate, imap_date, quote_mailbox


def _internaldate(testo: str):
    blob = f'1 (UID 3 RFC822.SIZE 100 INTERNALDATE "{testo}")'.encode()
    return _parse_internaldate(blob)


@contextlib.contextmanager
def fuso(nome: str):
    """Fa girare un blocco come se la macchina stesse in un altro fuso."""
    precedente = os.environ.get("TZ")
    os.environ["TZ"] = nome
    time.tzset()
    try:
        yield
    finally:
        if precedente is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = precedente
        time.tzset()


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


#: stesso istante scritto con tre offset diversi, e il suo valore in UTC
CASI = [
    ("05-Sep-2026 10:22:31 +0200", datetime(2026, 9, 5, 8, 22, 31, tzinfo=timezone.utc)),
    ("05-Sep-2026 10:22:31 -0500", datetime(2026, 9, 5, 15, 22, 31, tzinfo=timezone.utc)),
    ("05-Sep-2026 10:22:31 +0000", datetime(2026, 9, 5, 10, 22, 31, tzinfo=timezone.utc)),
]


@pytest.mark.parametrize("testo,atteso", CASI)
def test_internaldate_rispetta_l_offset_dichiarato(testo, atteso):
    assert _internaldate(testo) == atteso


def test_internaldate_non_dipende_dal_fuso_della_macchina():
    """Il difetto vero: la data veniva convertita con l'offset della VM invece
    che con quello dichiarato dal server, e usciva giusta solo in UTC."""
    for nome in ("UTC", "Europe/Rome", "America/New_York", "Asia/Kolkata"):
        with fuso(nome):
            for testo, atteso in CASI:
                assert _internaldate(testo) == atteso, f"sbagliata con TZ={nome}"


def test_internaldate_giorno_a_una_cifra():
    """RFC 3501: il giorno singolo è riempito con uno spazio, non con uno zero."""
    assert _internaldate(" 5-Sep-2026 10:22:31 +0200") == CASI[0][1]


def test_internaldate_assente():
    assert _parse_internaldate(b"1 (UID 3)") is None


@pytest.mark.parametrize("testo", [
    "32-Sep-2026 10:22:31 +0200",      # giorno inesistente
    "05-Set-2026 10:22:31 +0200",      # mese in italiano: IMAP li scrive in inglese
    "05-Sep-2026 10:22:31 +9900",      # offset oltre le 24 ore
    "05-Sep-2026 10:22:31",            # zona mancante
    "non una data",
])
def test_internaldate_malformata_non_inventa_una_data(testo):
    """Non sapere la data è già previsto a valle; inventarla no."""
    assert _internaldate(testo) is None


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
