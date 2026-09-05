"""Costruzione di buste PEC sintetiche.

Serve a provare il parsing e la logica di stato senza toccare un server IMAP.
Le forme riprodotte sono quelle che si incontrano davvero: busta di trasporto
firmata, busta di anomalia, ricevute positive e negative, oggetti RFC 2047,
charset dichiarati male, allegati con nomi impossibili su Windows.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

DATICERT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<postacert tipo="{tipo}" errore="{errore}">
  <intestazione>
    <mittente>{mittente}</mittente>
    <destinatari tipo="certificato">{destinatario}</destinatari>
    <risposte>{mittente}</risposte>
    <oggetto>{oggetto}</oggetto>
  </intestazione>
  <dati>
    <gestore-emittente>{gestore}</gestore-emittente>
    <data zona="+0200"><giorno>{giorno}</giorno><ora>{ora}</ora></data>
    <identificativo>{identificativo}</identificativo>
    <msgid>{msgid}</msgid>
    {extra}
  </dati>
</postacert>
"""


def daticert_grezzo(**kwargs) -> bytes:
    """daticert.xml malformato: msgid non escapato, come capita davvero."""
    return DATICERT_TEMPLATE.format(
        tipo=kwargs.get("tipo", "avvenuta-consegna"),
        errore=kwargs.get("errore", "nessuno"),
        mittente="studio@pec.it",
        destinatario="ente@pec.comune.it",
        oggetto="Istanza & documenti",
        gestore="Namirial S.p.A.",
        giorno="05/09/2026", ora="09:30:00",
        identificativo="opec123.4@pec.namirial.it",
        msgid="<non-escapato@pec.it>",
        extra="",
    ).encode("utf-8")


def daticert(tipo="posta-certificata", errore="nessuno",
             mittente="ente@pec.comune.it", destinatario="studio@pec.it",
             oggetto="Avviso di accertamento", gestore="Aruba PEC S.p.A.",
             giorno="05/09/2026", ora="10:22:31",
             identificativo="opec210101.20260905102231.01234.567.1.99@pec.aruba.it",
             msgid="<originale@pec.it>", extra="") -> bytes:
    from xml.sax.saxutils import escape

    return DATICERT_TEMPLATE.format(
        tipo=tipo, errore=errore, mittente=escape(mittente),
        destinatario=escape(destinatario), oggetto=escape(oggetto),
        gestore=escape(gestore), giorno=giorno, ora=ora,
        identificativo=escape(identificativo), msgid=escape(msgid), extra=extra,
    ).encode("utf-8")


def inner_message(subject="Avviso di accertamento n. 123/2026",
                  sender="ente@pec.comune.it", to="studio@pec.it",
                  body="Si notifica l'atto in allegato.\nCordiali saluti.",
                  html=None, attachments=(), charset="utf-8",
                  message_id="<originale@pec.it>",
                  date="Sat, 05 Sep 2026 10:22:00 +0200") -> bytes:
    """Il messaggio reale, quello che sta dentro postacert.eml."""
    if attachments or html:
        msg = MIMEMultipart("mixed")
        if html and body:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body, "plain", charset))
            alt.attach(MIMEText(html, "html", charset))
            msg.attach(alt)
        elif html:
            msg.attach(MIMEText(html, "html", charset))
        else:
            msg.attach(MIMEText(body, "plain", charset))
        for name, data, subtype in attachments:
            part = MIMEApplication(data, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=name)
            msg.attach(part)
    else:
        msg = MIMEText(body, "plain", charset)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = date
    msg["Message-ID"] = message_id
    return msg.as_bytes()


def _envelope(headers: dict, text: str, postacert: bytes | None,
              dati: bytes | None, signed=True) -> bytes:
    inner = MIMEMultipart("mixed")
    inner.attach(MIMEText(text, "plain", "utf-8"))
    if dati is not None:
        part = MIMEApplication(dati, _subtype="xml")
        part.add_header("Content-Disposition", "attachment", filename="daticert.xml")
        inner.attach(part)
    if postacert is not None:
        part = MIMEApplication(postacert, _subtype="octet-stream")
        part.add_header("Content-Disposition", "attachment", filename="postacert.eml")
        part.set_type("message/rfc822")
        inner.attach(part)

    if signed:
        outer = MIMEMultipart("signed", protocol="application/pkcs7-signature",
                              micalg="sha-256")
        outer.attach(inner)
        sig = MIMEApplication(b"finta-firma-del-gestore", _subtype="pkcs7-signature")
        sig.add_header("Content-Disposition", "attachment", filename="smime.p7s")
        outer.attach(sig)
        msg = outer
    else:
        msg = inner

    for key, value in headers.items():
        if value is not None:
            msg[key] = value
    return msg.as_bytes()


def busta_trasporto(subject="POSTA CERTIFICATA: Avviso di accertamento n. 123/2026",
                    postacert=None, dati=None, sender="posta-certificata@pec.aruba.it",
                    to="studio@pec.it", signed=True, **kwargs) -> bytes:
    """Busta di trasporto: il messaggio vero è dentro postacert.eml."""
    return _envelope(
        {
            "Subject": subject,
            "From": sender,
            "To": to,
            "Date": "Sat, 05 Sep 2026 10:22:31 +0200",
            "Message-ID": "<busta-1@pec.aruba.it>",
            "X-Trasporto": "posta-certificata",
            "X-Riferimento-Message-ID": kwargs.get("riferimento"),
        },
        "Messaggio di posta certificata",
        postacert if postacert is not None else inner_message(),
        dati if dati is not None else daticert(),
        signed=signed,
    )


def busta_anomalia(subject="ANOMALIA MESSAGGIO: Preventivo", postacert=None) -> bytes:
    """Mail non certificata reimbustata dal gestore."""
    return _envelope(
        {
            "Subject": subject,
            "From": "posta-certificata@pec.aruba.it",
            "To": "studio@pec.it",
            "Date": "Sat, 05 Sep 2026 11:00:00 +0200",
            "Message-ID": "<busta-anomalia@pec.aruba.it>",
            "X-Trasporto": "errore",
        },
        "Anomalia nel messaggio ricevuto: non conforme alle regole PEC",
        postacert if postacert is not None else inner_message(
            subject="Preventivo", sender="fornitore@gmail.com",
            body="In allegato il preventivo richiesto.",
            message_id="<anomalo@gmail.com>",
        ),
        None,
        signed=False,
    )


def ricevuta(tipo="accettazione", riferimento="<originale-studio@pec.it>",
             oggetto="Dichiarazione IVA", errore="nessuno",
             errore_esteso="", identificativo="opec999.1@pec.aruba.it") -> bytes:
    """Ricevuta del gestore su un messaggio inviato dallo studio."""
    prefix = {
        "accettazione": "ACCETTAZIONE",
        "presa-in-carico": "PRESA IN CARICO",
        "avvenuta-consegna": "CONSEGNA",
        "errore-consegna": "AVVISO DI MANCATA CONSEGNA",
        "non-accettazione": "AVVISO DI NON ACCETTAZIONE",
        "preavviso-errore-consegna": "PREAVVISO DI MANCATA CONSEGNA",
        "rilevazione-virus": "PROBLEMA DI SICUREZZA",
    }.get(tipo, tipo.upper())
    extra = '<ricevuta tipo="completa"/>'
    if errore_esteso:
        extra += f"<errore-esteso>{errore_esteso}</errore-esteso>"
    return _envelope(
        {
            "Subject": f"{prefix}: {oggetto}",
            "From": "posta-certificata@pec.aruba.it",
            "To": "studio@pec.it",
            "Date": "Sat, 05 Sep 2026 09:00:00 +0200",
            "Message-ID": f"<ricevuta-{tipo}@pec.aruba.it>",
            "X-Ricevuta": tipo,
            "X-Riferimento-Message-ID": riferimento,
            "X-TipoRicevuta": "completa",
        },
        f"Ricevuta di {tipo} per il messaggio con oggetto {oggetto}",
        None,
        daticert(tipo=tipo, errore=errore, oggetto=oggetto,
                 msgid=riferimento, identificativo=identificativo, extra=extra),
        signed=False,
    )


def messaggio_semplice(subject="Comunicazione di servizio",
                       sender="noreply@gestore.it") -> bytes:
    """Nessuna busta, nessuna ricevuta: caso 'generico'."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "studio@pec.it"
    msg["Date"] = "Sat, 05 Sep 2026 12:00:00 +0200"
    msg["Message-ID"] = "<generico@gestore.it>"
    msg.set_content("Manutenzione programmata del servizio.")
    return msg.as_bytes()
