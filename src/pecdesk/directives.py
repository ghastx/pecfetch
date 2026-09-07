"""Le direttive: due file, riletti a ogni esecuzione, sotto controllo di versione.

  * ``regole.toml``     certezze deterministiche, applicate in codice.
  * ``istruzioni.md``   sfumature in italiano, trasmesse al modello così come sono.

Sono due cose diverse e vanno tenute diverse. Una regola deterministica non si
affida a un giudizio; un'istruzione in linguaggio naturale esprime quello che una
regola non sa dire. La prima prevale sulla seconda quando non c'è un veto di
sicurezza (vedi ``decide.py``).

Ogni esito porta l'impronta delle direttive con cui è stato prodotto: senza,
rileggere il registro fra sei mesi non dice niente.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: instradamenti ammessi. Non è un elenco aperto: un instradamento che il codice
#: non conosce è un instradamento che non sa eseguire.
ROUTES = ("titolare", "studio", "cliente", "nessuno")

#: livelli di sospetto, dal più blando al più grave
SUSPICION_LEVELS = ("nessuno", "possibile", "probabile")

DEFAULT_DOC_TYPES = (
    "fattura", "sollecito_pagamento", "atto_giudiziario", "cartella_esattoriale",
    "avviso_accertamento", "notifica_ente", "contributi_inps",
    "comunicazione_bancaria", "contratto", "pratica_edilizia",
    "certificato_camerale", "ricevuta_negativa", "comunicazione_ordinaria",
    "altro",
)

DEFAULT_ALWAYS_OWNER = (
    "atto_giudiziario", "cartella_esattoriale", "avviso_accertamento",
)

#: parole attorno a cui si ritagliano le finestre di testo degli allegati.
#: Servono a smistare, non a leggere: dicono "qui c'è un termine" o "qui c'è una
#: richiesta di pagamento", non cosa dice il documento.
DEFAULT_KEYWORDS = (
    "entro il", "entro e non oltre", "termine", "termini", "scadenza", "scade",
    "ricorso", "opposizione", "impugnazione", "notifica", "notificazione",
    "diffida", "intimazione", "sollecito", "insoluto", "morosità", "decadenza",
    "sanzione", "sospensione", "udienza", "iban", "bonifico", "coordinate bancarie",
    "importo", "saldo", "prescrizione",
)


class DirectivesError(Exception):
    """Direttive assenti, malformate o incoerenti."""


@dataclass(frozen=True)
class Rule:
    id: str
    priority: int
    when: dict
    then: dict

    @property
    def route(self) -> str:
        return str(self.then.get("instradamento", "")).strip()

    @property
    def recipient(self) -> str:
        return str(self.then.get("a", "")).strip()

    @property
    def attention(self) -> bool:
        return bool(self.then.get("attenzione", False))

    @property
    def label(self) -> str:
        return str(self.then.get("etichetta", "")).strip()

    @property
    def skip_model(self) -> bool:
        return bool(self.then.get("salta_modello", False))

    @property
    def suspicion(self) -> str:
        return str(self.then.get("sospetto", "")).strip()


@dataclass(frozen=True)
class Directives:
    version: str
    rules: tuple[Rule, ...]
    doc_types: tuple[str, ...]
    always_owner_types: tuple[str, ...]
    keywords: tuple[str, ...]
    #: alias -> indirizzo. Il modello sceglie un alias, mai un indirizzo: così
    #: nessuna frase dentro un messaggio può dirottare un inoltro altrove.
    recipients: dict[str, str]
    instructions: str
    rules_digest: str
    instructions_digest: str
    revision: str = ""
    rules_path: Path | None = None
    instructions_path: Path | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def recipient_aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self.recipients))

    def resolve_recipient(self, alias: str) -> str:
        """Da alias a indirizzo. Un alias sconosciuto non diventa un indirizzo."""
        return self.recipients.get((alias or "").strip().lower(), "")

    def stamp(self) -> dict:
        """Come finisce dentro l'esito: leggibile fra sei mesi."""
        out = {"versione": self.version,
               "regole": self.rules_digest,
               "istruzioni": self.instructions_digest}
        if self.revision:
            out["revisione"] = self.revision
        return out


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


def _git_revision(path: Path) -> str:
    """Revisione git delle direttive, se ci sono. In produzione spesso non c'è."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _as_tuple(value, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DirectivesError(f"{what}: atteso un elenco")
    return tuple(str(v).strip() for v in value if str(v).strip())


def _validate_when(rule_id: str, when: dict) -> dict:
    known = {"mittente", "mittente_dominio", "casella", "cliente", "tipo_pecfetch",
             "oggetto_regex", "allegato_estensione", "allegato_nome_regex",
             "ha_allegati", "certificato"}
    unknown = set(when) - known
    if unknown:
        raise DirectivesError(
            f"regola '{rule_id}': condizioni sconosciute {sorted(unknown)}; "
            f"ammesse: {sorted(known)}"
        )
    for key in ("oggetto_regex", "allegato_nome_regex"):
        for pattern in when.get(key, []) or []:
            try:
                re.compile(str(pattern), re.IGNORECASE)
            except re.error as exc:
                raise DirectivesError(
                    f"regola '{rule_id}': espressione regolare non valida in "
                    f"{key}: {pattern} ({exc})"
                ) from exc
    return when


def _validate_then(rule_id: str, then: dict, recipients: dict) -> dict:
    known = {"instradamento", "a", "attenzione", "etichetta", "salta_modello",
             "sospetto"}
    unknown = set(then) - known
    if unknown:
        raise DirectivesError(
            f"regola '{rule_id}': azioni sconosciute {sorted(unknown)}; "
            f"ammesse: {sorted(known)}"
        )
    route = str(then.get("instradamento", "")).strip()
    if route and route not in ROUTES:
        raise DirectivesError(
            f"regola '{rule_id}': instradamento '{route}' non ammesso "
            f"(ammessi: {', '.join(ROUTES)})"
        )
    suspicion = str(then.get("sospetto", "")).strip()
    if suspicion and suspicion not in SUSPICION_LEVELS:
        raise DirectivesError(
            f"regola '{rule_id}': sospetto '{suspicion}' non ammesso "
            f"(ammessi: {', '.join(SUSPICION_LEVELS)})"
        )
    target = str(then.get("a", "")).strip()
    if target and "@" not in target and target.lower() not in recipients:
        raise DirectivesError(
            f"regola '{rule_id}': destinatario '{target}' non è né un indirizzo "
            f"né un alias dichiarato in [destinatari]"
        )
    if route in ("studio", "cliente") and not target:
        raise DirectivesError(
            f"regola '{rule_id}': instradamento '{route}' senza destinatario 'a'"
        )
    return then


def load_directives(rules_path: Path, instructions_path: Path) -> Directives:
    """Carica e valida le direttive. Un errore qui è fatale: si preferisce non
    lavorare piuttosto che lavorare con regole che non si sa cosa facciano."""
    try:
        rules_raw = Path(rules_path).read_bytes()
    except OSError as exc:
        raise DirectivesError(f"regole non leggibili: {exc}") from exc
    try:
        data = tomllib.loads(rules_raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise DirectivesError(f"regole non valide ({rules_path}): {exc}") from exc

    warnings: list[str] = []
    try:
        instructions_raw = Path(instructions_path).read_bytes()
        instructions = instructions_raw.decode("utf-8")
    except OSError as exc:
        raise DirectivesError(f"istruzioni non leggibili: {exc}") from exc

    recipients: dict[str, str] = {}
    for key, value in (data.get("destinatari", {}) or {}).items():
        if not isinstance(value, str) or "@" not in value:
            # in TOML una chiave scritta dopo un'intestazione di tabella finisce
            # dentro quella tabella: è l'errore facile da fare e impossibile da
            # vedere. Meglio fermarsi che trattare un elenco come un indirizzo.
            raise DirectivesError(
                f"[destinatari].{key} non è un indirizzo email: {value!r}. "
                "Le chiavi di primo livello (versione, tipi_documento, "
                "tipi_sempre_al_titolare) vanno dichiarate PRIMA di [destinatari]."
            )
        recipients[str(key).strip().lower()] = value.strip()
    if "titolare" not in recipients:
        raise DirectivesError(
            "[destinatari] deve contenere almeno 'titolare': è la destinazione "
            "di ogni incertezza e di ogni sospetto"
        )

    doc_types = _as_tuple(data.get("tipi_documento"), "tipi_documento") or DEFAULT_DOC_TYPES
    if "altro" not in doc_types:
        doc_types = doc_types + ("altro",)
    always_owner = _as_tuple(data.get("tipi_sempre_al_titolare"),
                             "tipi_sempre_al_titolare") or DEFAULT_ALWAYS_OWNER
    unknown_types = set(always_owner) - set(doc_types)
    if unknown_types:
        raise DirectivesError(
            f"tipi_sempre_al_titolare contiene tipi non dichiarati in "
            f"tipi_documento: {sorted(unknown_types)}"
        )

    keywords = _as_tuple(
        (data.get("parole_chiave", {}) or {}).get("termini"), "parole_chiave.termini"
    ) or DEFAULT_KEYWORDS

    rules: list[Rule] = []
    seen: set[str] = set()
    for position, raw in enumerate(data.get("regola", []) or []):
        rule_id = str(raw.get("id") or "").strip()
        if not rule_id:
            raise DirectivesError(f"la regola in posizione {position + 1} è priva di 'id'")
        if rule_id in seen:
            raise DirectivesError(f"id di regola duplicato: '{rule_id}'")
        seen.add(rule_id)
        when = _validate_when(rule_id, dict(raw.get("quando", {}) or {}))
        then = _validate_then(rule_id, dict(raw.get("allora", {}) or {}), recipients)
        if not when:
            warnings.append(f"regola '{rule_id}': nessuna condizione, vale per tutto")
        rules.append(Rule(id=rule_id, priority=int(raw.get("priorita", 0)),
                          when=when, then=then))
    # priorità alta prima; a parità, l'ordine di dichiarazione
    rules.sort(key=lambda r: -r.priority)

    return Directives(
        version=str(data.get("versione", "")).strip() or "senza-versione",
        rules=tuple(rules),
        doc_types=tuple(doc_types),
        always_owner_types=tuple(always_owner),
        keywords=tuple(k.lower() for k in keywords),
        recipients=recipients,
        instructions=instructions,
        rules_digest=_digest(rules_raw),
        instructions_digest=_digest(instructions_raw),
        revision=_git_revision(Path(rules_path)),
        rules_path=Path(rules_path),
        instructions_path=Path(instructions_path),
        warnings=tuple(warnings),
    )


__all__ = ["Directives", "DirectivesError", "Rule", "load_directives", "ROUTES",
           "SUSPICION_LEVELS", "DEFAULT_DOC_TYPES", "DEFAULT_KEYWORDS"]
