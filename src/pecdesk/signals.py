"""Segnali calcolati in codice, prima e indipendentemente dal modello.

Il segnale più solido contro la frode non è testuale ma relazionale: che quel
mittente non abbia mai scritto prima a quella casella. Si ricava dall'archivio
storico prodotto da pecfetch, interrogato **in sola lettura**.

Due avvertenze che il codice mette per iscritto:

* la PEC certifica il trasporto, non le intenzioni. `certificato: true` non è un
  indizio di affidabilità e qui non riduce mai il sospetto;
* il segnale relazionale ha una partenza a freddo. Su un archivio giovane ogni
  mittente è nuovo: sotto soglia di copertura il segnale viene emesso come
  `storico_insufficiente` e da solo non fa scattare niente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pecfetch import tempo

from .config import SuspicionLimits
from .queue import QueueItem

#: pesi dei segnali. Sommati danno il livello; le soglie stanno appena sotto.
WEIGHTS = {
    "mittente_mai_visto": 2,
    "mittente_sconosciuto_allo_studio": 1,
    # un archivio che pecfetch ha aperto e letto è traffico ordinario;
    # rar, 7z, immagini disco e archivi cifrati sono i formati che il malspam
    # usa per evadere i controlli, e pecfetch non li apre per scelta: basta
    # quello, anche da un mittente conosciuto
    "allegato_compresso": 2,
    "archivio_non_apribile": 4,
    "contenuto_attivo": 5,
    "contenuto_sospetto": 5,
    "virus_rilevato": 6,
    "tentata_istruzione": 5,
    "lessico_sollecito": 1,
    "urgenza": 1,
    "pressione_pagamento": 1,
    "busta_anomalia": 2,
    "non_certificato": 2,
}
#: La novità del mittente da sola non basta. Vale 2 (+1 se non lo si è mai
#: visto su nessuna casella): sotto soglia. Serve che si combini con qualcos
#: altro — un allegato compresso, un lessico da sollecito, un'urgenza — che è
#: esattamente come si presenta la frode vera. Senza questo margine il primo
#: fornitore nuovo di ogni cliente finirebbe fra i sospetti, e in tre giorni
#: il titolare smetterebbe di leggere quella sezione.
THRESHOLD_PROBABLE = 5
THRESHOLD_POSSIBLE = 4

_LEXICON_SOLICITATION = re.compile(
    r"(?i)\b(sollecit\w+|insolut\w+|diffid\w+|intimazion\w+|morosit\w+|"
    r"mancato pagamento|pagamento non pervenuto|ultimo avviso)\b"
)
_LEXICON_URGENCY = re.compile(
    r"(?i)\b(urgent\w+|immediat\w+|entro (?:24|48) ore|improrogabil\w+|"
    r"tassativ\w+|pena la sospensione)\b"
)
_LEXICON_PAYMENT = re.compile(
    r"(?i)\b(iban|bonific\w+|coordinate bancarie|nuove coordinate|"
    r"conto corrente|swift|bic)\b"
)

#: tentativi di parlare al modello invece che al destinatario. Non pretende di
#: essere esaustivo: quello che intercetta è comunque un segnale, e quello che
#: sfugge lo intercetta la delimitazione del materiale.
_INJECTION_PATTERNS = (
    (r"(?i)\bignor\w+\s+(?:le\s+)?(?:precedenti\s+)?istruzion", "ignora_istruzioni"),
    (r"(?i)\bignore\s+(?:all\s+|the\s+)?(?:previous|prior|above)\b", "ignore_previous"),
    (r"(?i)\bdisregard\s+(?:all\s+|the\s+)?(?:previous|prior|above)\b", "disregard"),
    (r"(?i)\b(?:sei|agisci come)\s+un(?:'|\s+)?(?:assistente|intelligenza artificiale|"
     r"modello linguistico)", "ruolo_imposto"),
    (r"(?i)\byou\s+are\s+(?:an?\s+)?(?:ai\s+)?(?:assistant|language model)\b", "ruolo_imposto"),
    (r"(?i)\b(?:system|developer)\s+prompt\b", "system_prompt"),
    (r"(?i)</?\s*(?:materiale_non_fidato|system|istruzioni)\b", "tag_falsificato"),
    (r"(?i)^\s*(?:human|assistant|system)\s*:", "marcatore_di_turno"),
    # solo le forme imperative a inizio riga: "vi invio la fattura a x@y.it" è
    # corrispondenza normale, non un tentativo di dirottare un inoltro
    (r"(?im)^\s*(?:inoltra(?:re|lo|la)?|gira(?:re)?|spedisci|forward)\s+"
     r"(?:quest[oa]|il|la|lo)?\s*"
     r"(?:messaggio|documento|mail|email|allegato|pec)?\s*a\s+\S+@",
     "istruzione_di_inoltro"),
    (r"(?i)\bnon\s+(?:mostrare|segnalare|riportare)\s+(?:questo|nulla)", "richiesta_di_silenzio"),
)
_INJECTION_RE = tuple((re.compile(p, re.MULTILINE), name)
                      for p, name in _INJECTION_PATTERNS)

#: archivi che pecfetch non apre per scelta, o che non si è potuto leggere
_OPAQUE_STATUSES = frozenset({"unsupported_archive", "encrypted", "archive_limit"})
_ARCHIVE_EXT = frozenset({"zip", "rar", "7z", "gz", "tgz", "bz2", "xz", "cab",
                          "iso", "img", "arj", "lzh", "ace"})


@dataclass
class History:
    """Quante volte quel mittente ha già scritto. Solo lettura, sempre."""

    seen_on_mailbox: int = 0
    seen_anywhere: int = 0
    mailbox_total: int = 0
    coverage_days: int = 0
    sufficient: bool = False

    @property
    def novel_on_mailbox(self) -> bool:
        return self.seen_on_mailbox == 0

    @property
    def novel_anywhere(self) -> bool:
        return self.seen_anywhere == 0


class ArchiveHistory:
    """Interroga l'archivio di pecfetch. Aperto ``mode=ro``: non può scriverci."""

    def __init__(self, archive_path: Path | str, limits: SuspicionLimits):
        from pecfetch.archive import Archive  # import pigro: pecdesk gira anche senza

        self.limits = limits
        self.archive = Archive(archive_path, read_only=True)
        self._coverage: dict[str, tuple[int, int]] = {}

    def close(self) -> None:
        self.archive.close()

    def __enter__(self) -> "ArchiveHistory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _mailbox_coverage(self, account: str) -> tuple[int, int]:
        """(messaggi sulla casella, giorni coperti dall'archivio)."""
        if account in self._coverage:
            return self._coverage[account]
        row = self.archive.db.execute(
            "SELECT COUNT(*), MIN(COALESCE(date_certified, fetched_at)), "
            "MAX(COALESCE(date_certified, fetched_at)) FROM messages WHERE account=?",
            (account,),
        ).fetchone()
        total = int(row[0] or 0)
        days = 0
        # `tempo.letto` e basta: tagliare l'offset con uno slice e trattare il
        # resto come naive sarebbe una seconda convenzione di lettura delle date
        # accanto a quella del contratto, e in archivio convivono ancora le righe
        # scritte prima che la convenzione ci fosse.
        first, last = tempo.letto(row[1]), tempo.letto(row[2])
        if first is not None and last is not None:
            days = max((last - first).days, 0)
        self._coverage[account] = (total, days)
        return total, days

    def lookup(self, item: QueueItem) -> History:
        total, days = self._mailbox_coverage(item.account)
        # Confronto fra stringhe, e qui resta tale: in SQLite queste colonne sono
        # TEXT, e riscriverlo in istanti vorrebbe dire leggere tutta la tabella
        # per contare due righe. Serve solo a non contare i messaggi arrivati
        # dopo (il messaggio stesso lo esclude già `id <> ?`), e lo scarto è
        # quello dichiarato nel README: un'ora, due volte l'anno, più le righe
        # scritte prima della convenzione sul fuso. Su un conteggio di
        # corrispondenza pregressa non sposta nulla; se un giorno servisse
        # esatto, il posto da cambiare è questo.
        before = item.date or "9999"
        on_mailbox = self.archive.db.execute(
            "SELECT COUNT(*) FROM messages WHERE account=? AND from_addr=? "
            "AND id<>? AND COALESCE(date_certified, fetched_at) < ?",
            (item.account, item.sender, item.id, before),
        ).fetchone()[0]
        anywhere = self.archive.db.execute(
            "SELECT COUNT(*) FROM messages WHERE from_addr=? AND id<>? "
            "AND COALESCE(date_certified, fetched_at) < ?",
            (item.sender, item.id, before),
        ).fetchone()[0]
        sufficient = (total >= self.limits.min_history_messages
                      and days >= self.limits.min_history_days)
        return History(
            seen_on_mailbox=int(on_mailbox),
            seen_anywhere=int(anywhere),
            mailbox_total=total,
            coverage_days=days,
            sufficient=sufficient,
        )


@dataclass
class Signals:
    """I segnali del messaggio, con il perché di ciascuno."""

    names: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)
    score: int = 0
    level: str = "nessuno"
    history: History = field(default_factory=History)
    injection_hits: list[str] = field(default_factory=list)

    def add(self, name: str, detail: str = "") -> None:
        if name in self.names:
            return
        self.names.append(name)
        if detail:
            self.details[name] = detail
        self.score += WEIGHTS.get(name, 0)

    def note(self, name: str, detail: str = "") -> None:
        """Segnale informativo: si riporta ma non pesa."""
        if name not in self.names:
            self.names.append(name)
        if detail:
            self.details[name] = detail

    def finalize(self) -> "Signals":
        if self.score >= THRESHOLD_PROBABLE:
            self.level = "probabile"
        elif self.score >= THRESHOLD_POSSIBLE:
            self.level = "possibile"
        else:
            self.level = "nessuno"
        return self

    def as_dict(self) -> dict:
        out = {"livello": self.level, "segnali": list(self.names),
               "punteggio": self.score}
        if self.details:
            out["dettagli"] = dict(self.details)
        return out


def detect_injection(text: str) -> list[str]:
    """Frasi che tentano di parlare al programma invece che al destinatario.

    Un messaggio che ci prova è di per sé un segnale, e va riportato come tale:
    non cambia le direttive — quelle non sono cambiabili dal contenuto — ma dice
    qualcosa su chi l'ha spedito.
    """
    hits: list[str] = []
    for pattern, name in _INJECTION_RE:
        if pattern.search(text) and name not in hits:
            hits.append(name)
    return hits


def collect(item: QueueItem, history: History, text: str,
            declared_sender: bool = False) -> Signals:
    """Calcola i segnali. `text` è oggetto + corpo + estratti già montati.

    `declared_sender` dice che una regola deterministica nomina questo mittente
    o il suo dominio: il titolare lo ha dichiarato, quindi non è un estraneo
    per quanto sia la prima volta che scrive. Non abbassa nient'altro — un
    allegato attivo, un archivio che non si apre o un tentativo di istruzione
    pesano uguale, ed è quello che protegge dalla casella compromessa di uno
    che si conosce.
    """
    sig = Signals(history=history)

    # -- relazionali -----------------------------------------------------
    if declared_sender:
        sig.note("mittente_dichiarato_nelle_regole",
                 "una regola nomina questo mittente o il suo dominio")
    elif history.sufficient:
        if history.novel_on_mailbox:
            sig.add("mittente_mai_visto",
                    f"mai scritto a {item.account} (storico: "
                    f"{history.mailbox_total} messaggi, {history.coverage_days} giorni)")
        if history.novel_anywhere:
            sig.add("mittente_sconosciuto_allo_studio",
                    "mai visto su nessuna casella dello studio")
    elif history.novel_on_mailbox:
        sig.note("storico_insufficiente",
                 f"mittente nuovo, ma l'archivio copre solo "
                 f"{history.mailbox_total} messaggi in {history.coverage_days} "
                 f"giorni su {item.account}: il segnale non è affidabile")

    # -- esito dell'analisi fatta a monte da pecfetch ---------------------
    if item.msg_type == "rilevazione_virus":
        sig.add("virus_rilevato", "ricevuta di rilevazione virus dal gestore")
    if item.msg_type == "busta_anomalia":
        sig.add("busta_anomalia", "busta di anomalia: la PEC non è certificata")
    elif not item.certified and item.msg_type == "generico":
        sig.add("non_certificato", "messaggio non certificato su casella PEC")
    for note in item.notes:
        if "virus" in note.lower():
            sig.add("virus_rilevato", note)

    # -- allegati ---------------------------------------------------------
    for att in item.attachments:
        if att.active:
            sig.add("contenuto_attivo",
                    f"{att.name}: tipo attivo, non materializzato "
                    f"({att.note or 'eseguibile o script'})")
        if att.suspicious:
            sig.add("contenuto_sospetto",
                    f"{att.name}: il nome mente sul contenuto ({att.note})")
        if att.status in _OPAQUE_STATUSES:
            sig.add("archivio_non_apribile", f"{att.name}: {att.status}")
        elif att.is_archive or att.extension in _ARCHIVE_EXT:
            sig.add("allegato_compresso", f"{att.name}: allegato compresso")

    # -- lessico -----------------------------------------------------------
    haystack = f"{item.subject}\n{text}"
    if _LEXICON_SOLICITATION.search(haystack):
        sig.add("lessico_sollecito", "lessico di insoluto o sollecito")
    if _LEXICON_URGENCY.search(haystack):
        sig.add("urgenza", "pressione temporale nel testo")
    if _LEXICON_PAYMENT.search(haystack):
        sig.add("pressione_pagamento", "coordinate bancarie o richiesta di pagamento")

    # -- tentativi di dirottare il programma -------------------------------
    hits = detect_injection(haystack)
    if hits:
        sig.injection_hits = hits
        sig.add("tentata_istruzione",
                "il messaggio contiene frasi rivolte al programma: "
                + ", ".join(hits))

    return sig.finalize()


__all__ = ["ArchiveHistory", "History", "Signals", "collect", "detect_injection",
           "WEIGHTS", "THRESHOLD_POSSIBLE", "THRESHOLD_PROBABLE"]
