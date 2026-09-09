"""L'unica email che pecdesk spedisce: il riepilogo al titolare.

Il vincolo del progetto è "non invia messaggi per conto di nessuno", e il
riepilogo non è un'eccezione mascherata: è un messaggio con un solo destinatario,
letto dalla configurazione, **mai** dedotto dal contenuto di una PEC, mai una
risposta a qualcosa, mai su una casella PEC.

Quel vincolo è scritto qui come codice, non come buona intenzione: ``send()``
rifiuta più di un destinatario e non accetta ``In-Reply-To`` né ``References``.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from pecfetch import permessi as perm

from .config import DigestSettings

log = logging.getLogger("pecdesk.mailer")


class MailError(Exception):
    """Il riepilogo non è partito."""


def build_message(settings: DigestSettings, subject: str, text: str,
                  html_body: str = "") -> EmailMessage:
    if not settings.to:
        raise MailError("nessun destinatario configurato per il riepilogo")
    if "," in settings.to or ";" in settings.to:
        raise MailError("il riepilogo ha un solo destinatario, sempre")

    message = EmailMessage()
    message["From"] = settings.sender or settings.smtp_user or settings.to
    message["To"] = settings.to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="pecdesk.local")
    # non è una risposta a niente e non deve poter essere agganciato a un thread
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text)
    if html_body:
        message.add_alternative(
            f"<!doctype html><html><body>{html_body}</body></html>",
            subtype="html",
        )
    return message


def write_copy(directory: Path, day: str, text: str, html_body: str,
               permissions: perm.Permessi | None = None) -> Path:
    """Copia su file. Utile per diagnosi, e per chi il riepilogo lo vuole lì.

    Il riepilogo contiene oggetti e mittenti di PEC: vale il modello di permessi
    del resto dell'albero, non quello che capita dalla umask.
    """
    permissions = permissions or perm.Permessi()
    directory = Path(directory)
    perm.crea_dir(directory, permissions.dir_mode, permissions)
    txt = directory / f"{day}.txt"
    txt.write_text(text, encoding="utf-8")
    perm.applica_file(txt, permissions)
    if html_body:
        html = directory / f"{day}.html"
        html.write_text(
            f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"</head><body>{html_body}</body></html>",
            encoding="utf-8",
        )
        perm.applica_file(html, permissions)
    return txt


def send(settings: DigestSettings, message: EmailMessage) -> None:
    """Consegna via submission. Un solo destinatario, quello configurato."""
    recipients = message.get_all("To") or []
    if len(recipients) != 1 or recipients[0] != settings.to:
        raise MailError("destinatario del riepilogo alterato: invio annullato")
    if not settings.smtp_host:
        raise MailError("nessun server SMTP configurato")

    try:
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      timeout=settings.smtp_timeout)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                                  timeout=settings.smtp_timeout)
        with server:
            server.ehlo()
            if settings.smtp_starttls and not settings.smtp_ssl:
                server.starttls()
                server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"invio del riepilogo fallito: {exc}") from exc


def check(settings: DigestSettings) -> list[str]:
    """Verifica la raggiungibilità del server. Non spedisce niente."""
    problems: list[str] = []
    if not settings.send:
        return ["invio disattivato: il riepilogo viene solo scritto su file"]
    if not settings.to:
        problems.append("destinatario non configurato")
    if not settings.smtp_host:
        problems.append("server SMTP non configurato")
        return problems
    try:
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      timeout=settings.smtp_timeout)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                                  timeout=settings.smtp_timeout)
        with server:
            server.ehlo()
            if settings.smtp_starttls and not settings.smtp_ssl:
                server.starttls()
                server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
    except (smtplib.SMTPException, OSError) as exc:
        problems.append(f"SMTP non raggiungibile: {exc}")
    return problems


__all__ = ["MailError", "build_message", "send", "check", "write_copy"]
