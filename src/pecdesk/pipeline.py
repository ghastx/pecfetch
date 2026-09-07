"""Orchestrazione: dalla coda all'esito, e dall'esito al riepilogo.

L'ordine degli effetti è quello che rende l'idempotenza visibile, ed è scritto
una volta sola, qui:

    riconcilia → per ogni messaggio: claim → materiale → modello →
    riga di esito (fsync) → classificato → rename → archiviato    → riepilogo

Degradare, non fingere: un guasto sul singolo messaggio lo lascia in coda e lo
annota; un guasto sistemico (chiave rifiutata, credito esaurito, cinque
fallimenti di fila) ferma le chiamate ma **non** il riepilogo, che parte lo
stesso dicendo cosa è arrivato senza giudizio e perché.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .decide import compose
from .digest import Digest, Unworked
from .digest import build as build_digest
from .digest import render_html, render_text
from .directives import Directives
from .mailer import MailError, build_message, send, write_copy
from .material import build as build_material
from .model import (Classifier, ModelError, ModelRefused, ModelStop,
                    ModelUnavailable, PROMPT_VERSION, Request, build_request)
from .outcomes import Outcome, OutcomeStore
from .queue import QueueItem, move_to_worked, scan
from .rules import RuleMatch, apply_rules
from .signals import ArchiveHistory, History, Signals, collect
from .state import State

log = logging.getLogger("pecdesk.pipeline")

#: quanti giorni di esiti si rileggono per riconciliare lo stato
RECONCILE_DAYS = 7


@dataclass
class RunSummary:
    classified: int = 0
    failed: int = 0
    skipped: int = 0
    moved: int = 0
    reconciled: int = 0
    stopped: str = ""
    problems: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    unworked: list[Unworked] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0

    @property
    def ok(self) -> bool:
        return not self.stopped and not self.failed


class Runner:
    """Tiene insieme configurazione, direttive, stato e classificatore."""

    def __init__(self, cfg: Config, directives: Directives, state: State,
                 store: OutcomeStore, classifier: Classifier | None = None,
                 history: ArchiveHistory | None = None,
                 classifier_error: str = ""):
        self.cfg = cfg
        self.directives = directives
        self.state = state
        self.store = store
        self.classifier = classifier
        self.history = history
        #: perché il classificatore manca, per dirlo a chi legge il riepilogo
        self.classifier_error = classifier_error

    # -- preparazione di un singolo messaggio ------------------------------
    def prepare(self, item: QueueItem) -> tuple[Signals, Request, "RuleMatch"]:
        """Segnali, regole e richiesta. Nessun effetto: la prova si ferma qui."""
        material = build_material(item, self.cfg.material, self.directives.keywords)
        hist = (self.history.lookup(item) if self.history is not None else History())
        rules = apply_rules(self.directives, item)
        signals = collect(item, hist, material.scan_text,
                          declared_sender=rules.declared_sender)
        request = build_request(item, signals, material, self.directives,
                                self.cfg.model, self.cfg.max_tokens)
        return signals, request, rules

    # -- lavorazione -------------------------------------------------------
    def reconcile(self, summary: RunSummary) -> None:
        """Un esito su disco è la prova che il messaggio è stato lavorato."""
        written = self.store.known_ids(days=RECONCILE_DAYS)
        fixed = self.state.reconcile(written)
        summary.reconciled = len(fixed)
        if fixed:
            log.info("riconciliati %d messaggi già classificati ma non registrati",
                     len(fixed))

    def move_pending(self, items_by_id: dict[str, QueueItem],
                     summary: RunSummary) -> None:
        """Chi ha già l'esito scritto va solo tolto dalla coda."""
        for msg_id in self.state.classified_not_archived():
            item = items_by_id.get(msg_id)
            if item is None:
                self.state.mark_archived(msg_id)   # già sparito dalla coda
                continue
            try:
                move_to_worked(item.path, self.cfg.worked_dir)
            except OSError as exc:
                summary.problems.append(f"{msg_id}: spostamento fallito ({exc})")
                continue
            self.state.mark_archived(msg_id)
            summary.moved += 1

    def classify_one(self, item: QueueItem) -> Outcome:
        """Un messaggio, un esito. Solleva ModelError se non ce la fa."""
        signals, request, rules = self.prepare(item)
        assert request.material is not None
        material = request.material

        judgment = None
        model_info: dict = {}
        if not rules.skip_model:
            if self.classifier is None:
                raise ModelStop(self.classifier_error
                                or "nessun classificatore configurato")
            judgment = self.classifier.classify(request)
            model_info = {
                "id": self.cfg.model,
                "prompt": PROMPT_VERSION,
                "token_in": judgment.tokens_in,
                "token_out": judgment.tokens_out,
                "cache_read": judgment.cache_read,
            }
        else:
            model_info = {"id": "nessuno", "motivo": "regola deterministica",
                          "prompt": PROMPT_VERSION}

        return compose(
            item=item, rules=rules, signals=signals, judgment=judgment,
            material=material, directives=self.directives,
            processed_at=self.store.now().isoformat(timespec="seconds"),
            model_info=model_info,
        )

    def commit(self, item: QueueItem, outcome: Outcome) -> None:
        """L'effetto si registra prima di produrlo, e nell'ordine giusto."""
        path = self.store.append(outcome)
        self.state.mark_classified(outcome.id, path.name, outcome.klass)
        try:
            move_to_worked(item.path, self.cfg.worked_dir)
        except OSError as exc:
            log.warning("%s: spostamento rimandato (%s)", outcome.id, exc)
            return
        self.state.mark_archived(outcome.id)

    def run(self, limit: int | None = None) -> RunSummary:
        summary = RunSummary()
        items, problems = scan(self.cfg.queue_dir)
        summary.problems += problems

        by_id = {it.id: it for it in items}
        self.reconcile(summary)
        self.move_pending(by_id, summary)

        budget = limit if limit is not None else self.cfg.api.max_messages
        consecutive = 0
        processed = 0
        untouched: list[QueueItem] = []

        for index, item in enumerate(items):
            if summary.stopped or processed >= budget:
                untouched += items[index:]
                break
            claim = self.state.claim(item.id, item.account, self.cfg.max_attempts)
            if not claim.proceed:
                if claim.state in ("sospeso",):
                    summary.unworked.append(Unworked(
                        id=item.id, mailbox=item.mailbox_label, sender=item.sender,
                        subject=item.subject, reason=claim.reason,
                    ))
                summary.skipped += 1
                continue

            processed += 1
            try:
                outcome = self.classify_one(item)
            except ModelStop as exc:
                # guasto sistemico: si smette di chiamare, non si inventa niente
                self.state.fail(item.id, str(exc), self.cfg.max_attempts)
                summary.stopped = str(exc)
                summary.unworked.append(Unworked(
                    id=item.id, mailbox=item.mailbox_label, sender=item.sender,
                    subject=item.subject, reason=str(exc),
                ))
                log.error("interrotto: %s", exc)
                untouched += items[index + 1:]
                break
            except ModelRefused as exc:
                # ritentarla non serve: si sospende e si va avanti
                self.state.suspend(item.id, str(exc))
                summary.failed += 1
                summary.unworked.append(Unworked(
                    id=item.id, mailbox=item.mailbox_label, sender=item.sender,
                    subject=item.subject, reason=str(exc),
                ))
                log.warning("%s: %s", item.id, exc)
                continue
            except (ModelUnavailable, ModelError) as exc:
                state = self.state.fail(item.id, str(exc), self.cfg.max_attempts)
                summary.failed += 1
                consecutive += 1
                summary.unworked.append(Unworked(
                    id=item.id, mailbox=item.mailbox_label, sender=item.sender,
                    subject=item.subject, reason=str(exc),
                ))
                log.warning("%s: %s (stato: %s)", item.id, exc, state)
                if consecutive >= self.cfg.api.stop_after_failures:
                    summary.stopped = (
                        f"{consecutive} fallimenti consecutivi: "
                        f"non è un caso, è un guasto"
                    )
                    log.error(summary.stopped)
                    untouched += items[index + 1:]
                    break
                continue
            except Exception as exc:                       # noqa: BLE001
                # un messaggio illeggibile non deve fermare la notte
                self.state.fail(item.id, repr(exc), self.cfg.max_attempts)
                summary.failed += 1
                summary.unworked.append(Unworked(
                    id=item.id, mailbox=item.mailbox_label, sender=item.sender,
                    subject=item.subject, reason=f"errore interno: {exc}",
                ))
                log.exception("%s: errore interno", item.id)
                continue

            consecutive = 0
            self.commit(item, outcome)
            summary.classified += 1
            summary.outcomes.append(outcome)
            summary.tokens_in += int(outcome.model.get("token_in", 0) or 0)
            summary.tokens_out += int(outcome.model.get("token_out", 0) or 0)
            summary.cache_read += int(outcome.model.get("cache_read", 0) or 0)
            if self.cfg.api.pause_seconds:
                time.sleep(self.cfg.api.pause_seconds)

        # ciò che resta in coda e non è stato nemmeno tentato
        already = {u.id for u in summary.unworked}
        for item in untouched:
            if item.id in already:
                continue
            summary.skipped += 1
            summary.unworked.append(Unworked(
                id=item.id, mailbox=item.mailbox_label, sender=item.sender,
                subject=item.subject,
                reason=summary.stopped or "non lavorato in questa esecuzione",
            ))
        return summary

    # -- riepilogo ---------------------------------------------------------
    def digest_for(self, summary: RunSummary, day: date | None = None) -> Digest:
        day = day or self.store.now().date()
        notes: list[str] = []
        if summary.stopped:
            notes.append(f"classificazione interrotta: {summary.stopped}")
        for problem in summary.problems[:3]:
            notes.append(problem)
        return build_digest(summary.outcomes, summary.unworked, day=day, notes=notes)

    def send_digest(self, digest: Digest, force: bool = False) -> tuple[bool, str]:
        """Prenota, poi spedisce. Al massimo una volta al giorno."""
        cfg = self.cfg.digest
        day = f"{digest.day:%Y-%m-%d}"
        if force:
            self.state.force_digest(day)
        if not self.state.claim_digest(day, cfg.to):
            return False, "riepilogo del giorno già prenotato o già inviato"

        text = render_text(digest)
        html_body = render_html(digest)
        if cfg.copy_dir:
            try:
                write_copy(cfg.copy_dir, day, text, html_body)
            except OSError as exc:
                log.warning("copia del riepilogo non scritta: %s", exc)

        if not cfg.send:
            self.state.mark_digest_sent(day, str(digest.counts()))
            return True, "riepilogo scritto su file (invio disattivato)"

        try:
            message = build_message(cfg, digest.subject_line(cfg.subject_prefix),
                                    text, html_body)
            send(cfg, message)
        except MailError as exc:
            # la prenotazione si rilascia solo se non si è spedito niente
            self.state.release_digest(day)
            return False, str(exc)
        self.state.mark_digest_sent(day, str(digest.counts()))
        return True, f"riepilogo inviato a {cfg.to}"


def make_classifier(cfg: Config) -> Classifier:
    """Il classificatore vero. Senza chiave non si prova nemmeno a costruirlo."""
    from .model import AnthropicClassifier

    if not cfg.api_key:
        raise ModelStop(
            "chiave API assente: impostala con [modello].api_key_env o "
            "api_key_file, mai nel repository"
        )
    return AnthropicClassifier(cfg.api_key, max_retries=cfg.api.max_retries,
                               timeout=cfg.api.timeout_seconds)


__all__ = ["Runner", "RunSummary", "make_classifier", "RECONCILE_DAYS"]
