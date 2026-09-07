"""Composizione dell'esito: dove il veto, la regola e il giudizio si incontrano.

L'ordine è questo e non un altro:

1. il sospetto è il **massimo** fra i segnali calcolati in codice, quello
   dichiarato da una regola e quello del modello — mai la media, mai il minimo;
2. **sospetto ⇒ veto**: nessun inoltro automatico, si segnala al titolare. Il
   veto prevale anche su una regola deterministica di inoltro;
3. altrimenti la **regola deterministica** vince su istruzioni in linguaggio
   naturale e giudizio del modello;
4. i **tipi che vanno sempre al titolare** sono la rete di sicurezza contro
   l'estratto: se una regola ha già deciso altro, resta almeno l'attenzione;
5. altrimenti vale l'**instradamento del modello**;
6. **confidenza bassa o instradamento assente ⇒ titolare**, dichiarato incerto:
   l'incertezza non si maschera da decisione;
7. **termine non verificabile ⇒ intervento del titolare**. L'assenza di prova fa
   salire di livello, non scendere: un termine mancato costa molto più di un
   termine segnalato a torto.

È tutta una funzione pura: non tocca la rete, non tocca il disco, e si prova per
intero senza chiamare niente.
"""

from __future__ import annotations

from .directives import Directives
from .material import Material
from .model import Judgment
from .outcomes import Outcome
from .queue import QueueItem
from .rules import RuleMatch
from .signals import Signals

_LEVELS = {"nessuno": 0, "possibile": 1, "probabile": 2}
_CONFIDENCE_ORDER = {"alta": 2, "media": 1, "bassa": 0}


def _worst(*levels: str) -> str:
    best = max((_LEVELS.get(lv, 0) for lv in levels if lv), default=0)
    for name, value in _LEVELS.items():
        if value == best:
            return name
    return "nessuno"


def _cap_confidence(current: str, ceiling: str) -> str:
    if _CONFIDENCE_ORDER.get(current, 0) > _CONFIDENCE_ORDER.get(ceiling, 0):
        return ceiling
    return current


def compose(item: QueueItem, rules: RuleMatch, signals: Signals,
            judgment: Judgment | None, material: Material,
            directives: Directives, processed_at: str,
            model_info: dict | None = None, error: str = "") -> Outcome:
    """Da segnali, regole e giudizio a un esito. Nessun effetto collaterale."""
    outcome = Outcome(
        id=item.id,
        processed_at=processed_at,
        account=item.account,
        client=item.client,
        mailbox=item.mailbox_label,
        sender=item.sender,
        subject=item.subject[:200],
        date=item.date[:19],
        rules_applied=list(rules.matched),
        label=rules.label,
        directives=directives.stamp(),
        model=dict(model_info or {}),
        material=material.as_dict(),
        error=error,
    )

    # -- 0. un elemento non lavorato non riceve una classificazione inventata
    if judgment is None and not rules.any:
        outcome.klass = "non_classificato"
        outcome.route = "titolare"
        outcome.recipient_alias = "titolare"
        outcome.recipient = directives.resolve_recipient("titolare")
        outcome.route_origin = "incertezza"
        outcome.owner_attention = True
        outcome.uncertain = True
        outcome.confidence = "bassa"
        outcome.uncertainty = ["non_classificato"]
        outcome.suspicion_level = signals.level
        outcome.suspicion_signals = list(signals.names)
        outcome.suspicion_details = dict(signals.details)
        outcome.reason = error or "non classificato"
        return outcome

    # -- 1. sospetto: il massimo, mai la media -----------------------------
    model_suspicion = judgment.suspicion if judgment else "nessuno"
    level = _worst(signals.level, rules.suspicion, model_suspicion)
    suspicion_signals = list(signals.names)
    for name in (judgment.suspicion_signals if judgment else []):
        if name not in suspicion_signals:
            suspicion_signals.append(f"modello:{name}")
    if judgment is not None and judgment.instruction_attempt:
        if "tentata_istruzione" not in suspicion_signals:
            suspicion_signals.append("tentata_istruzione")
        # un messaggio che prova a dare ordini al programma è per ciò stesso
        # sospetto, anche se non ha nessun altro segnale
        level = _worst(level, "possibile")
    if material.tag_forgery and "tag_falsificato" not in suspicion_signals:
        suspicion_signals.append("tag_falsificato")
        level = _worst(level, "possibile")
    if rules.suspicion and rules.suspicion != "nessuno":
        suspicion_signals.append(f"regola:{rules.deciding or ','.join(rules.matched)}")

    outcome.suspicion_level = level
    outcome.suspicion_signals = suspicion_signals
    outcome.suspicion_details = dict(signals.details)
    outcome.doc_type = judgment.doc_type if judgment else "altro"

    # -- confidenza: quella del modello, poi abbassata da com'è il materiale.
    # Senza modello ma con una regola che decide non c'è nessun dubbio: la
    # regola è una certezza, ed è esattamente per questo che si è potuto non
    # chiamare nessuno.
    confidence = judgment.confidence if judgment else ("alta" if rules.route
                                                       else "bassa")
    uncertainty = list(judgment.uncertainty) if judgment else []

    def reason_once(name: str) -> None:
        if name not in uncertainty:
            uncertainty.append(name)

    if material.truncations:
        reason_once("solo_estratto")
    if material.ocr_attachments:
        reason_once("ocr_incerto")
        confidence = _cap_confidence(confidence, "media")
    if material.missing_text:
        reason_once("testo_allegato_assente")
    if item.attachments and not any(a.has_text for a in item.attachments) \
            and material.body_chars < 200:
        # niente corpo e niente testo negli allegati: si giudica quasi sul nulla
        reason_once("materiale_quasi_assente")
        confidence = _cap_confidence(confidence, "bassa")
    if signals.history.sufficient and signals.history.novel_on_mailbox:
        reason_once("mittente_sconosciuto")

    # -- 2. veto di sicurezza ---------------------------------------------
    if _LEVELS[level] >= _LEVELS["possibile"]:
        outcome.klass = "sospetto"
        outcome.route = "titolare"
        outcome.recipient_alias = "titolare"
        outcome.recipient = directives.resolve_recipient("titolare")
        outcome.route_origin = "veto"
        outcome.owner_attention = True
        outcome.confidence = confidence
        outcome.uncertainty = uncertainty
        outcome.uncertain = confidence == "bassa"
        outcome.reason = (judgment.reason if judgment and judgment.reason
                          else "messaggio sospetto: non si propone un inoltro")
        outcome.deadline = _deadline(judgment, material, forced=False)
        if outcome.deadline:
            outcome.deadline["verificato"] = bool(outcome.deadline.get("verificato"))
        return outcome

    # -- 3. regola deterministica ------------------------------------------
    route = ""
    alias = ""
    origin = ""
    if rules.route:
        route = rules.route
        alias = rules.recipient_alias
        origin = "regola"
    # -- 4. tipi che vanno sempre al titolare (rete contro l'estratto) -----
    always_owner = outcome.doc_type in directives.always_owner_types
    if always_owner and not route:
        route = "titolare"
        alias = "titolare"
        origin = "tipo"
    # -- 5. giudizio del modello -------------------------------------------
    if not route and judgment is not None and judgment.route:
        route = judgment.route
        alias = judgment.recipient_alias
        origin = "modello"

    owner_attention = bool(rules.attention)
    if judgment is not None and judgment.owner_attention:
        owner_attention = True
    if always_owner:
        # una regola può instradare altrove, ma non può far sparire il fatto che
        # su questo tipo l'estratto non basta a escludere un termine
        owner_attention = True

    # -- 6. incertezza -----------------------------------------------------
    uncertain = False
    if not route or confidence == "bassa":
        uncertain = True
        if not route:
            reason_once("instradamento_non_determinato")
        route = "titolare"
        alias = "titolare"
        origin = "incertezza"
        owner_attention = True

    # -- 7. termine ---------------------------------------------------------
    deadline = _deadline(judgment, material, forced=always_owner)
    if deadline is not None and not deadline.get("verificato"):
        owner_attention = True

    if route == "titolare":
        alias = "titolare"
    resolved = directives.resolve_recipient(alias) if alias else ""
    if route in ("studio", "cliente") and not resolved:
        # un inoltro senza indirizzo non è eseguibile: diventa un dubbio
        route, alias, origin = "titolare", "titolare", "incertezza"
        resolved = directives.resolve_recipient("titolare")
        owner_attention = True
        uncertain = True
        reason_once("destinatario_non_risolto")

    outcome.route = route
    outcome.recipient_alias = alias
    outcome.recipient = resolved
    outcome.route_origin = origin
    outcome.owner_attention = owner_attention
    outcome.deadline = deadline
    outcome.confidence = confidence
    outcome.uncertainty = uncertainty
    outcome.uncertain = uncertain
    outcome.reason = (judgment.reason if judgment and judgment.reason
                      else _rule_reason(rules, directives))
    outcome.klass = _classify(outcome)
    return outcome


def _deadline(judgment: Judgment | None, material: Material,
              forced: bool) -> dict | None:
    """Il termine, con la sua verificabilità. Il silenzio non è una prova."""
    present = bool(judgment and judgment.deadline_present)
    if not present and not forced:
        return None
    date = (judgment.deadline_date if judgment else "").strip()
    what = (judgment.deadline_what if judgment else "").strip()
    from_ocr = bool(material.ocr_attachments)
    # una data che viene solo da un OCR incerto non è una data: è un indizio
    verified = bool(date) and not from_ocr
    entry: dict = {"presente": True, "data": date or None,
                   "cosa": what or "", "verificato": verified}
    if not present and forced:
        entry["cosa"] = what or "possibile termine non verificabile sull'estratto"
        entry["origine"] = "tipo_documento"
    if from_ocr:
        entry["ocr"] = True
    if material.truncations:
        entry["su_estratto"] = True
    return entry


def _rule_reason(rules: RuleMatch, directives: Directives) -> str:
    if rules.deciding:
        return f"regola «{rules.deciding}»"
    if rules.matched:
        return "regole: " + ", ".join(rules.matched)
    return "nessuna regola applicabile"


def _classify(outcome: Outcome) -> str:
    if outcome.suspicion_level != "nessuno":
        return "sospetto"
    if outcome.owner_attention or outcome.route == "titolare":
        return "attenzione"
    if outcome.route in ("studio", "cliente"):
        return "inoltro"
    return "ordinario"


__all__ = ["compose"]
