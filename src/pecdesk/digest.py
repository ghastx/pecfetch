"""Il riepilogo giornaliero: deve leggersi in trenta secondi da un telefono.

Quindi: righe corte, una riga per messaggio, l'ordine deciso dall'urgenza e non
dall'orologio, e niente tabelle. Prima quello che può fare male — i sospetti e i
termini — poi quello che va guardato, poi le proposte di inoltro, e in fondo
l'ordinario ridotto a un conteggio.

Se qualcosa non è stato lavorato, il riepilogo lo dice. Una mattina senza email è
peggio di una mattina con "12 messaggi arrivati, non classificati".
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from pecfetch import tempo

from .outcomes import Outcome

MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
          "agosto", "settembre", "ottobre", "novembre", "dicembre")
WEEKDAYS = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato",
            "domenica")

_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_IT_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
#: "30 settembre 2026", come lo scrivono gli atti e come lo ricopia il modello
_IT_WORDS = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(
        ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
         "agosto", "settembre", "ottobre", "novembre", "dicembre")) +
    r")\b", re.IGNORECASE)


def italian_date(value: date | datetime) -> str:
    return f"{WEEKDAYS[value.weekday()]} {value.day} {MONTHS[value.month - 1]}"


def short_date(raw: str) -> str:
    """Da qualunque cosa il modello abbia scritto a gg/mm, se si può."""
    if not raw:
        return ""
    match = _ISO_DATE.search(raw)
    if match:
        return f"{int(match.group(3)):02d}/{int(match.group(2)):02d}"
    match = _IT_DATE.search(raw)
    if match:
        return f"{int(match.group(1)):02d}/{int(match.group(2)):02d}"
    match = _IT_WORDS.search(raw)
    if match:
        mese = MONTHS.index(match.group(2).lower()) + 1
        return f"{int(match.group(1)):02d}/{mese:02d}"
    # data che non si riconosce: si mostra intera, non tagliata a metà
    return raw.strip()


def _quando(outcome: Outcome) -> tuple:
    """Ordine cronologico, non alfabetico.

    `outcome.date` esce dal record d'indice con l'offset del fuso dichiarato, e
    due ISO con offset diversi non si ordinano bene come stringhe: nell'ora del
    ritorno all'ora solare `02:30+02:00` precede `02:30+01:00` ma ordina dopo. È
    la stessa regola che `queue._ordine` applica alla coda.
    """
    letto = tempo.letto(outcome.date)
    return (0, letto.timestamp()) if letto else (1, 0.0)


def _sort_key(outcome: Outcome) -> tuple:
    deadline = outcome.deadline or {}
    raw = str(deadline.get("data") or "")
    match = _ISO_DATE.search(raw)
    if match:
        return (0, match.group(0))
    match = _IT_DATE.search(raw)
    if match:
        year = int(match.group(3))
        year += 2000 if year < 100 else 0
        return (0, f"{year:04d}-{int(match.group(2)):02d}-{int(match.group(1)):02d}")
    match = _IT_WORDS.search(raw)
    if match:
        mese = MONTHS.index(match.group(2).lower()) + 1
        anno = re.search(r"\b(20\d{2})\b", raw)
        return (0, f"{anno.group(1) if anno else '9999'}-{mese:02d}-"
                   f"{int(match.group(1)):02d}")
    # nessuna data di termine riconosciuta: si ripiega sull'arrivo, a istanti
    return (1,) + _quando(outcome)


@dataclass
class Unworked:
    """Un elemento arrivato ma non giudicato, con il perché."""

    id: str
    mailbox: str = ""
    sender: str = ""
    subject: str = ""
    reason: str = ""


@dataclass
class Digest:
    day: date
    suspicious: list[Outcome] = field(default_factory=list)
    deadlines: list[Outcome] = field(default_factory=list)
    attention: list[Outcome] = field(default_factory=list)
    forwards: list[Outcome] = field(default_factory=list)
    ordinary: list[Outcome] = field(default_factory=list)
    unworked: list[Unworked] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (len(self.suspicious) + len(self.deadlines) + len(self.attention)
                + len(self.forwards) + len(self.ordinary) + len(self.unworked))

    def counts(self) -> dict:
        return {"sospetti": len(self.suspicious), "termini": len(self.deadlines),
                "attenzione": len(self.attention), "inoltri": len(self.forwards),
                "ordinari": len(self.ordinary), "non_lavorati": len(self.unworked),
                "totale": self.total}

    def subject_line(self, prefix: str = "PEC") -> str:
        bits: list[str] = []
        if self.suspicious:
            bits.append(f"{len(self.suspicious)} sospett"
                        f"{'o' if len(self.suspicious) == 1 else 'i'}")
        if self.deadlines:
            bits.append(f"{len(self.deadlines)} termin"
                        f"{'e' if len(self.deadlines) == 1 else 'i'}")
        if self.unworked:
            bits.append(f"{len(self.unworked)} non lavorat"
                        f"{'o' if len(self.unworked) == 1 else 'i'}")
        head = f"{prefix} {self.day.day} {MONTHS[self.day.month - 1]}"
        tail = ", ".join(bits) if bits else f"{self.total} messaggi"
        return f"{head} — {tail}"


def build(outcomes, unworked=None, day: date | None = None,
          notes=None) -> Digest:
    """Ordina gli esiti per urgenza. Un messaggio compare in una sezione sola."""
    # i chiamanti passano sempre il giorno del fuso dichiarato; questo è solo
    # il ripiego per l'uso diretto della funzione
    digest = Digest(day=day or date.today(), unworked=list(unworked or []),
                    notes=list(notes or []))
    for outcome in outcomes:
        if outcome.klass == "non_classificato":
            digest.unworked.append(Unworked(
                id=outcome.id, mailbox=outcome.mailbox, sender=outcome.sender,
                subject=outcome.subject,
                reason=outcome.error or outcome.reason or "non classificato",
            ))
        elif outcome.suspicion_level != "nessuno":
            digest.suspicious.append(outcome)
        elif outcome.deadline is not None:
            digest.deadlines.append(outcome)
        elif outcome.klass == "attenzione":
            digest.attention.append(outcome)
        elif outcome.klass == "inoltro":
            digest.forwards.append(outcome)
        else:
            digest.ordinary.append(outcome)

    digest.suspicious.sort(key=_quando)
    digest.deadlines.sort(key=_sort_key)
    digest.attention.sort(key=_quando)
    digest.forwards.sort(key=lambda o: (o.recipient_alias,) + _quando(o))
    digest.ordinary.sort(key=lambda o: (o.doc_type,) + _quando(o))
    return digest


def _who(outcome: Outcome) -> str:
    return outcome.client or outcome.account or outcome.mailbox or "?"


def _subject(outcome: Outcome, width: int = 58) -> str:
    subject = (outcome.subject or "(senza oggetto)").replace("\n", " ").strip()
    return subject[:width] + ("…" if len(subject) > width else "")


def _deadline_label(outcome: Outcome) -> str:
    deadline = outcome.deadline or {}
    shown = short_date(str(deadline.get("data") or ""))
    if not shown:
        return "da verificare"
    if not deadline.get("verificato"):
        return f"{shown}?"
    return shown


def _type_label(doc_type: str, label: str = "") -> str:
    """Quando il tipo non dice niente, l'etichetta della regola dice di più."""
    if (not doc_type or doc_type == "altro") and label:
        return label.replace("_", " ").replace("-", " ")
    return (doc_type or "altro").replace("_", " ")


# ---------------------------------------------------------------------------
# testo semplice
# ---------------------------------------------------------------------------

def render_text(digest: Digest) -> str:
    lines: list[str] = [
        f"PEC {italian_date(digest.day)} — {digest.total} messaggi",
    ]
    for note in digest.notes:
        lines.append(f"! {note}")
    lines.append("")

    if digest.suspicious:
        lines.append(f"SOSPETTI ({len(digest.suspicious)})")
        for o in digest.suspicious:
            lines.append(f"• {_who(o)} ← {o.sender}")
            lines.append(f"  «{_subject(o)}»")
            signals = ", ".join(o.suspicion_signals[:3]) or o.suspicion_level
            lines.append(f"  {signals}")
        lines.append("")

    if digest.deadlines:
        lines.append(f"TERMINI ({len(digest.deadlines)})")
        for o in digest.deadlines:
            what = (o.deadline or {}).get("cosa") or _type_label(o.doc_type, o.label)
            lines.append(f"• {_deadline_label(o):>13}  {_who(o)} — {what}")
            lines.append(f"  «{_subject(o)}»")
        lines.append("")

    if digest.attention:
        lines.append(f"DA VEDERE ({len(digest.attention)})")
        for o in digest.attention:
            lines.append(f"• {_who(o)} — {_type_label(o.doc_type, o.label)}")
            lines.append(f"  «{_subject(o)}» — {o.reason[:70]}")
        lines.append("")

    if digest.forwards:
        lines.append(f"INOLTRI PROPOSTI ({len(digest.forwards)})")
        for o in digest.forwards:
            lines.append(f"• {_who(o)} → {o.recipient_alias or o.recipient} "
                         f"— {_type_label(o.doc_type, o.label)}")
            lines.append(f"  «{_subject(o)}»")
        lines.append("")

    if digest.ordinary:
        by_type: dict[str, int] = {}
        for o in digest.ordinary:
            by_type[o.doc_type] = by_type.get(o.doc_type, 0) + 1
        summary = ", ".join(f"{_type_label(t)} ({n})"
                            for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]))
        lines.append(f"ORDINARI ({len(digest.ordinary)}): {summary}")
        lines.append("")

    if digest.unworked:
        lines.append(f"NON LAVORATI ({len(digest.unworked)})")
        for u in digest.unworked:
            lines.append(f"• {u.mailbox or '?'} ← {u.sender} — {u.reason[:70]}")
            if u.subject:
                lines.append(f"  «{u.subject[:58]}»")
        lines.append("")

    if digest.total == 0:
        lines.append("Niente in coda.")
    lines.append("— pecdesk (proposte, non decisioni: niente è stato inoltrato)")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_STYLE = """
body{margin:0;padding:16px;background:#fbfbf9;color:#1a1a1a;
 font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1{font-size:18px;margin:0 0 4px}
h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;
 margin:22px 0 8px;color:#666;font-weight:600}
ul{list-style:none;margin:0;padding:0}
li{padding:8px 0 8px 12px;border-left:3px solid #ddd;margin-bottom:6px}
li.sospetto{border-color:#b3261e;background:#fdf3f2}
li.termine{border-color:#a8620a;background:#fdf7ef}
li.attenzione{border-color:#6b5bd2}
li.inoltro{border-color:#2c7a4b}
li.guasto{border-color:#888;background:#f2f2f2}
.chi{font-weight:600}
.meta{color:#5a5a5a;font-size:14px}
.ogg{color:#333}
.quando{display:inline-block;min-width:78px;font-weight:600;color:#a8620a}
.nota{color:#b3261e;font-weight:600;margin:0 0 10px}
.piede{margin-top:26px;color:#777;font-size:13px;border-top:1px solid #e3e3e0;
 padding-top:10px}
"""


def _esc(value: str) -> str:
    return html.escape(value or "", quote=False)


def render_html(digest: Digest) -> str:
    parts = [f"<style>{_STYLE}</style>",
             f"<h1>PEC {_esc(italian_date(digest.day))} — {digest.total} messaggi</h1>"]
    for note in digest.notes:
        parts.append(f'<p class="nota">! {_esc(note)}</p>')

    if digest.suspicious:
        parts.append(f"<h2>Sospetti ({len(digest.suspicious)})</h2><ul>")
        for o in digest.suspicious:
            signals = _esc(", ".join(o.suspicion_signals[:3]) or o.suspicion_level)
            parts.append(
                f'<li class="sospetto"><span class="chi">{_esc(_who(o))}</span> '
                f'&larr; {_esc(o.sender)}<br><span class="ogg">'
                f'«{_esc(_subject(o))}»</span><br>'
                f'<span class="meta">{signals}</span></li>'
            )
        parts.append("</ul>")

    if digest.deadlines:
        parts.append(f"<h2>Termini ({len(digest.deadlines)})</h2><ul>")
        for o in digest.deadlines:
            what = _esc((o.deadline or {}).get("cosa")
                        or _type_label(o.doc_type, o.label))
            parts.append(
                f'<li class="termine"><span class="quando">{_esc(_deadline_label(o))}'
                f'</span> <span class="chi">{_esc(_who(o))}</span> — {what}<br>'
                f'<span class="ogg">«{_esc(_subject(o))}»</span></li>'
            )
        parts.append("</ul>")

    if digest.attention:
        parts.append(f"<h2>Da vedere ({len(digest.attention)})</h2><ul>")
        for o in digest.attention:
            parts.append(
                f'<li class="attenzione"><span class="chi">{_esc(_who(o))}</span> — '
                f'{_esc(_type_label(o.doc_type, o.label))}<br>'
                f'<span class="ogg">«{_esc(_subject(o))}»</span><br>'
                f'<span class="meta">{_esc(o.reason[:90])}</span></li>'
            )
        parts.append("</ul>")

    if digest.forwards:
        parts.append(f"<h2>Inoltri proposti ({len(digest.forwards)})</h2><ul>")
        for o in digest.forwards:
            parts.append(
                f'<li class="inoltro"><span class="chi">{_esc(_who(o))}</span> '
                f'&rarr; {_esc(o.recipient_alias or o.recipient)} — '
                f'{_esc(_type_label(o.doc_type, o.label))}<br>'
                f'<span class="ogg">«{_esc(_subject(o))}»</span></li>'
            )
        parts.append("</ul>")

    if digest.ordinary:
        by_type: dict[str, int] = {}
        for o in digest.ordinary:
            by_type[o.doc_type] = by_type.get(o.doc_type, 0) + 1
        summary = ", ".join(f"{_type_label(t)} ({n})"
                            for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]))
        parts.append(f"<h2>Ordinari ({len(digest.ordinary)})</h2>"
                     f'<p class="meta">{_esc(summary)}</p>')

    if digest.unworked:
        parts.append(f"<h2>Non lavorati ({len(digest.unworked)})</h2><ul>")
        for u in digest.unworked:
            parts.append(
                f'<li class="guasto"><span class="chi">{_esc(u.mailbox or "?")}</span> '
                f'&larr; {_esc(u.sender)}<br>'
                f'<span class="meta">{_esc(u.reason[:90])}</span></li>'
            )
        parts.append("</ul>")

    if digest.total == 0:
        parts.append('<p class="meta">Niente in coda.</p>')
    parts.append('<p class="piede">pecdesk — proposte, non decisioni: '
                 "niente è stato inoltrato e nessuna casella PEC è stata toccata.</p>")
    return "\n".join(parts)


__all__ = ["Digest", "Unworked", "build", "render_text", "render_html",
           "italian_date", "short_date"]
