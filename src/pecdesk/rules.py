"""Applicazione delle regole deterministiche.

Mittente, dominio, casella ricevente, tipo di documento: sono certezze, e una
certezza non si affida a un giudizio. Qui non si chiama nessun modello.

Una regola con priorità più alta decide l'instradamento; tutte le regole che
fanno match contribuiscono comunque attenzione ed eventuale sospetto, perché
quelli sono pavimenti, non alternative: se una regola dice "guardalo", nessuna
altra regola deve poterlo far sparire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .directives import Directives, Rule
from .queue import QueueItem


@dataclass
class RuleMatch:
    """Cosa hanno detto le regole, prima che intervenga qualunque giudizio."""

    matched: list[str] = field(default_factory=list)
    route: str = ""
    recipient_alias: str = ""
    attention: bool = False
    label: str = ""
    skip_model: bool = False
    suspicion: str = "nessuno"
    #: quale regola ha deciso l'instradamento
    deciding: str = ""
    #: una regola ha nominato questo mittente o il suo dominio? Allora il
    #: titolare lo conosce: non è un estraneo, per quanto nuovo
    declared_sender: bool = False

    @property
    def any(self) -> bool:
        return bool(self.matched)


def _lower_set(values) -> set[str]:
    return {str(v).strip().lower() for v in (values or []) if str(v).strip()}


def _matches_any_regex(patterns, text: str) -> bool:
    for pattern in patterns or []:
        try:
            if re.search(str(pattern), text, re.IGNORECASE):
                return True
        except re.error:
            # le espressioni sono già validate al caricamento; se una sfugge,
            # non far esplodere una lavorazione notturna per un carattere
            continue
    return False


def rule_applies(rule: Rule, item: QueueItem) -> bool:
    """Tutte le condizioni in AND; dentro una condizione, i valori in OR."""
    when = rule.when
    if not when:
        return True

    if "mittente" in when:
        if item.sender not in _lower_set(when["mittente"]):
            return False
    if "mittente_dominio" in when:
        wanted = _lower_set(when["mittente_dominio"])
        domain = item.sender_domain
        # un dominio dichiarato copre anche i suoi sottodomini
        if not any(domain == d or domain.endswith("." + d) for d in wanted):
            return False
    if "casella" in when:
        if item.account.lower() not in _lower_set(when["casella"]):
            return False
    if "cliente" in when:
        if item.client.lower() not in _lower_set(when["cliente"]):
            return False
    if "tipo_pecfetch" in when:
        if item.msg_type.lower() not in _lower_set(when["tipo_pecfetch"]):
            return False
    if "certificato" in when:
        if bool(item.certified) is not bool(when["certificato"]):
            return False
    if "ha_allegati" in when:
        if bool(item.attachments) is not bool(when["ha_allegati"]):
            return False
    if "oggetto_regex" in when:
        if not _matches_any_regex(when["oggetto_regex"], item.subject):
            return False
    if "allegato_estensione" in when:
        wanted = {e.lstrip(".") for e in _lower_set(when["allegato_estensione"])}
        if not any(a.extension in wanted for a in item.attachments):
            return False
    if "allegato_nome_regex" in when:
        if not any(_matches_any_regex(when["allegato_nome_regex"], a.name)
                   for a in item.attachments):
            return False
    return True


def apply_rules(directives: Directives, item: QueueItem) -> RuleMatch:
    result = RuleMatch()
    levels = {"nessuno": 0, "possibile": 1, "probabile": 2}
    for rule in directives.rules:          # già ordinate per priorità decrescente
        if not rule_applies(rule, item):
            continue
        result.matched.append(rule.id)
        if "mittente" in rule.when or "mittente_dominio" in rule.when:
            result.declared_sender = True
        if rule.attention:
            result.attention = True
        if rule.suspicion and levels.get(rule.suspicion, 0) > levels[result.suspicion]:
            result.suspicion = rule.suspicion
        if rule.route and not result.route:
            # la prima regola che decide (priorità più alta) vince
            result.route = rule.route
            result.recipient_alias = rule.recipient
            result.label = rule.label
            result.skip_model = rule.skip_model
            result.deciding = rule.id
        elif not rule.route and rule.skip_model:
            result.skip_model = True
        if rule.label and not result.label:
            result.label = rule.label
    return result


__all__ = ["RuleMatch", "apply_rules", "rule_applies"]
