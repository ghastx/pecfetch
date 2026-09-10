"""Interfaccia a riga di comando di pecdesk.

Codici di uscita (l'esecuzione è periodica e non presidiata, quindi contano):

    0  tutto bene
    2  completato, ma con messaggi non lavorati o riepilogo non spedito
    1  errore fatale (configurazione, direttive, stato)
    3  un'altra esecuzione era già in corso
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date

from pecfetch import permessi as perm, spazio, tempo
from pecfetch.lock import AlreadyRunning, RunLock
from pecfetch.logging_setup import setup as setup_logging

from . import __version__
from .config import Config, ConfigError, load_config, redacted
from .digest import Unworked
from .digest import build as build_digest
from .digest import render_text
from .directives import Directives, DirectivesError, load_directives
from .mailer import check as smtp_check
from .material import build as build_material
from .model import ModelStop, build_request
from .outcomes import OutcomeStore
from .pipeline import Runner, make_classifier
from .queue import scan
from .rules import apply_rules
from .signals import ArchiveHistory, History, collect
from .state import State

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2
EXIT_LOCKED = 3

log = logging.getLogger("pecdesk")


# ---------------------------------------------------------------------------
# argomenti
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pecdesk",
        description="Smista la coda prodotta da pecfetch: classifica i messaggi, "
                    "scrive gli esiti e manda al titolare il riepilogo del giorno. "
                    "Non tocca le caselle PEC e non inoltra niente.",
    )
    parser.add_argument("--version", action="version", version=f"pecdesk {__version__}")
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="file di configurazione TOML")
    parser.add_argument("-v", "--verbose", action="store_true", help="log di debug")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="su console solo avvisi ed errori")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="FILE",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    _sub = parser.add_subparsers(dest="command", required=True)

    class sub:
        @staticmethod
        def add_parser(name, **kwargs):
            kwargs.setdefault("parents", []).append(common)
            return _sub.add_parser(name, **kwargs)

    p_run = sub.add_parser("run", help="lavora la coda e manda il riepilogo")
    p_run.add_argument("--limite", type=int, metavar="N",
                       help="massimo messaggi da classificare")
    p_run.add_argument("--prova", action="store_true",
                       help="mostra cosa verrebbe inviato al modello senza "
                            "chiamare l'API, senza scrivere e senza spostare nulla")
    p_run.add_argument("--conta-token", action="store_true",
                       help="con --prova, conta i token davvero (usa l'API di "
                            "conteggio, non il modello)")
    p_run.add_argument("--senza-riepilogo", action="store_true",
                       help="non spedire il riepilogo")

    p_dig = sub.add_parser("riepilogo", help="ricompone e spedisce il riepilogo")
    p_dig.add_argument("--giorno", metavar="AAAA-MM-GG", help="giorno da riepilogare")
    p_dig.add_argument("--forza", action="store_true",
                       help="rispedisci anche se già prenotato o inviato")
    p_dig.add_argument("--stampa", action="store_true",
                       help="stampa e basta, non spedire")

    p_exp = sub.add_parser("spiega", help="mostra l'esito e il materiale di un messaggio")
    p_exp.add_argument("id", help="identificativo del messaggio")
    p_exp.add_argument("--materiale", action="store_true",
                       help="mostra anche cosa verrebbe inviato al modello")

    p_fix = sub.add_parser("correggi", help="registra una correzione accanto all'esito")
    p_fix.add_argument("id")
    p_fix.add_argument("--campo", required=True,
                       help="campo corretto (es. instradamento, tipo_documento)")
    p_fix.add_argument("--valore", required=True, help="valore corretto")
    p_fix.add_argument("--nota", default="", help="perché")
    p_fix.add_argument("--autore", default="", help="chi corregge")

    p_reg = sub.add_parser("registro", help="dove il sistema sbaglia in modo sistematico")
    p_reg.add_argument("--giorni", type=int, help="ultimi N giorni")
    p_reg.add_argument("--da", metavar="AAAA-MM-GG")
    p_reg.add_argument("--a", metavar="AAAA-MM-GG")
    p_reg.add_argument("--json", action="store_true")

    p_chk = sub.add_parser("check", help="verifica configurazione, direttive e collegamenti")
    p_chk.add_argument("--api", action="store_true", help="prova anche la chiave API")
    p_chk.add_argument("--smtp", action="store_true", help="prova anche l'SMTP")

    sub.add_parser("stato", help="stato dei messaggi e ultime esecuzioni")

    p_retry = sub.add_parser("riprova", help="sblocca un messaggio sospeso")
    p_retry.add_argument("id", nargs="+")

    return parser


# ---------------------------------------------------------------------------
# supporto
# ---------------------------------------------------------------------------

def _load(args) -> tuple[Config, Directives]:
    warnings: list[str] = []
    cfg = load_config(getattr(args, "config", None), warn=warnings.append)
    # come in pecfetch: la umask non deve concedere più del modello dichiarato
    os.umask(perm.umask_da(cfg.permissions))
    setup_logging(cfg.log_level, cfg.log_file,
                  quiet=getattr(args, "quiet", False),
                  verbose=getattr(args, "verbose", False),
                  timezone_name=cfg.timezone)
    for message in warnings:
        log.warning("%s", message)
    directives = load_directives(cfg.rules_path, cfg.instructions_path)
    for message in directives.warnings:
        log.warning("direttive: %s", message)
    return cfg, directives


def _open_history(cfg: Config) -> ArchiveHistory | None:
    try:
        return ArchiveHistory(cfg.archive_path, cfg.suspicion)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("archivio storico non disponibile (%s): il segnale "
                    "relazionale sarà assente", exc)
        return None


def _out(text: str = "") -> None:
    print(text)


# ---------------------------------------------------------------------------
# comandi
# ---------------------------------------------------------------------------

def cmd_run(args, cfg: Config, directives: Directives) -> int:
    if args.prova:
        return _dry_run(args, cfg, directives)

    with State(cfg.state_path, cfg.timezone, cfg.permissions) as state:
        history = _open_history(cfg)
        classifier_error = ""
        try:
            classifier = make_classifier(cfg)
        except ModelStop as exc:
            # non si può classificare, ma il riepilogo deve partire lo stesso:
            # dire "sono arrivati 12 messaggi, nessuno classificato, ecco perché"
            # è comunque un servizio
            classifier, classifier_error = None, str(exc)
            log.error("%s", exc)
            _out(f"! {exc}")
        store = OutcomeStore(cfg.outcomes_dir, cfg.timezone, cfg.permissions)
        runner = Runner(cfg, directives, state, store, classifier, history,
                        classifier_error=classifier_error)

        run_id = state.start_run()
        summary = runner.run(limit=args.limite)
        if classifier is None and not summary.stopped:
            summary.stopped = classifier_error
        state.finish_run(run_id, summary.classified, summary.failed,
                         summary.skipped, summary.stopped)
        if history is not None:
            history.close()

        _out(f"classificati {summary.classified}, falliti {summary.failed}, "
             f"non lavorati {summary.skipped}, spostati {summary.moved}")
        if summary.reconciled:
            _out(f"riconciliati {summary.reconciled} esiti già scritti")
        if summary.tokens_in:
            _out(f"token: {summary.tokens_in} in ({summary.cache_read} da cache), "
                 f"{summary.tokens_out} out")
        for problem in summary.problems:
            _out(f"! {problem}")
        if summary.stopped:
            _out(f"! interrotto: {summary.stopped}")

        if args.senza_riepilogo:
            return EXIT_PARTIAL if (summary.failed or summary.stopped) else EXIT_OK

        # il riepilogo parte comunque: anche dire "non ho classificato niente,
        # ecco cosa è arrivato" è un servizio
        digest = runner.digest_for(summary)
        sent, message = runner.send_digest(digest)
        _out(("riepilogo: " if sent else "! riepilogo: ") + message)
        if not sent or summary.failed or summary.stopped:
            return EXIT_PARTIAL
    return EXIT_OK


def _dry_run(args, cfg: Config, directives: Directives) -> int:
    """Cosa verrebbe inviato, senza inviare niente e senza toccare niente."""
    items, problems = scan(cfg.queue_dir, limit=args.limite)
    for problem in problems:
        _out(f"! {problem}")
    if not items:
        _out("coda vuota")
        return EXIT_OK

    history = _open_history(cfg)
    counter = None
    if args.conta_token:
        try:
            counter = make_classifier(cfg)
        except ModelStop as exc:
            _out(f"! conteggio token non disponibile: {exc}")

    total_estimate = 0
    for item in items:
        material = build_material(item, cfg.material, directives.keywords)
        hist = history.lookup(item) if history is not None else History()
        rules = apply_rules(directives, item)
        signals = collect(item, hist, material.scan_text,
                          declared_sender=rules.declared_sender)
        request = build_request(item, signals, material, directives, cfg.model,
                                cfg.max_tokens)

        _out("=" * 78)
        _out(f"{item.id}  [{item.account}]  {item.date[:16]}")
        _out(f"  mittente: {item.sender}")
        _out(f"  oggetto:  {item.subject[:70]}")
        _out(f"  regole:   {', '.join(rules.matched) or 'nessuna'}"
             + (f"  → {rules.route}" if rules.route else ""))
        _out(f"  segnali:  {', '.join(signals.names) or 'nessuno'} "
             f"(livello: {signals.level}, punteggio {signals.score})")
        _out(f"  storico:  {hist.seen_on_mailbox} sulla casella, "
             f"{hist.seen_anywhere} in tutto"
             + ("" if hist.sufficient else "  [archivio sotto soglia]"))
        if material.truncations:
            for cut in material.truncations:
                _out(f"  taglio:   {cut['cosa']}: inviati {cut['inviati']} "
                     f"di {cut['totali']}")
        if material.inventory_only:
            _out(f"  solo inventario: {', '.join(material.inventory_only)}")
        if rules.skip_model:
            _out("  → nessuna chiamata: la regola decide da sola")
            continue

        estimate = request.estimated_tokens()
        if counter is not None:
            try:
                estimate = counter.count_tokens(request)
                _out(f"  token:    {estimate} (contati)")
            except Exception as exc:                          # noqa: BLE001
                _out(f"  token:    ~{estimate} (stima; conteggio fallito: {exc})")
        else:
            _out(f"  token:    ~{estimate} (stima offline)")
        total_estimate += estimate
        _out("  ---- materiale non fidato che verrebbe inviato ----")
        for line in material.untrusted.splitlines():
            _out(f"  | {line}")

    _out("=" * 78)
    _out(f"{len(items)} messaggi, ~{total_estimate} token di input in totale "
         f"(il blocco di sistema è identico e va in cache dopo il primo)")
    _out("prova: nessuna chiamata al modello, nessun esito scritto, "
         "niente spostato dalla coda")
    if history is not None:
        history.close()
    return EXIT_OK


def cmd_digest(args, cfg: Config, directives: Directives) -> int:
    # il giorno del fuso dichiarato: lo stesso che nomina i file degli esiti
    day = date.fromisoformat(args.giorno) if args.giorno else tempo.oggi(cfg.tz)
    iso = f"{day:%Y-%m-%d}"
    store = OutcomeStore(cfg.outcomes_dir, cfg.timezone, cfg.permissions)
    outcomes = store.read(since=iso, until=iso)

    with State(cfg.state_path, cfg.timezone, cfg.permissions) as state:
        # quello che è ancora in coda senza esito è, per definizione, non lavorato
        judged = {o.id for o in outcomes}
        items, problems = scan(cfg.queue_dir)
        errors = {row["id"]: (row["ultimo_errore"] or "sospeso")
                  for row in state.suspended()}
        unworked = [
            Unworked(id=item.id, mailbox=item.mailbox_label, sender=item.sender,
                     subject=item.subject,
                     reason=errors.get(item.id, "ancora in coda, non classificato"))
            for item in items if item.id not in judged
        ]
        digest = build_digest(outcomes, unworked, day=day, notes=problems[:3])
        if args.stampa:
            _out(render_text(digest))
            return EXIT_OK
        runner = Runner(cfg, directives, state, store)
        sent, message = runner.send_digest(digest, force=args.forza)
        _out(("riepilogo: " if sent else "! riepilogo: ") + message)
        return EXIT_OK if sent else EXIT_PARTIAL


def cmd_explain(args, cfg: Config, directives: Directives) -> int:
    store = OutcomeStore(cfg.outcomes_dir, cfg.timezone, cfg.permissions)
    found = store.find(args.id)
    if found:
        for outcome in found:
            _out(json.dumps(outcome.to_json(), ensure_ascii=False, indent=1))
    else:
        _out(f"nessun esito per {args.id}")

    corrections = [c for c in store.read_corrections() if c.get("id") == args.id]
    for correction in corrections:
        _out("correzione: " + json.dumps(correction, ensure_ascii=False))

    if not args.materiale:
        return EXIT_OK if found else EXIT_PARTIAL

    items, _ = scan(cfg.queue_dir)
    item = next((i for i in items if i.id == args.id), None)
    if item is None:
        _out("il messaggio non è più in coda: il materiale non è ricostruibile")
        return EXIT_OK
    history = _open_history(cfg)
    material = build_material(item, cfg.material, directives.keywords)
    hist = history.lookup(item) if history is not None else History()
    rules = apply_rules(directives, item)
    signals = collect(item, hist, material.scan_text,
                      declared_sender=rules.declared_sender)
    request = build_request(item, signals, material, directives, cfg.model,
                            cfg.max_tokens)
    _out("---- blocco di sistema ----")
    _out(request.system)
    _out("---- messaggio ----")
    _out(request.user)
    if history is not None:
        history.close()
    return EXIT_OK


def cmd_correct(args, cfg: Config, directives: Directives) -> int:
    store = OutcomeStore(cfg.outcomes_dir, cfg.timezone, cfg.permissions)
    found = store.find(args.id)
    if not found:
        _out(f"nessun esito per {args.id}: non c'è niente da correggere")
        return EXIT_PARTIAL
    outcome = found[-1]
    proposed = outcome.to_json().get(args.campo)
    if proposed is None and args.campo in outcome.to_json().get("instradamento", {}):
        proposed = outcome.to_json()["instradamento"][args.campo]
    path = store.append_correction(args.id, args.campo, proposed, args.valore,
                                   author=args.autore, note=args.nota)
    _out(f"correzione registrata in {path.name}: {args.campo} "
         f"{proposed!r} → {args.valore!r}")
    _out("la proposta originale resta dov'era: il registro serve a vedere "
         "dove il sistema sbaglia, non a nasconderlo")
    return EXIT_OK


def cmd_register(args, cfg: Config, directives: Directives) -> int:
    store = OutcomeStore(cfg.outcomes_dir, cfg.timezone, cfg.permissions)
    outcomes = store.read(days=args.giorni, since=args.da or "", until=args.a or "")
    corrections = store.read_corrections(days=args.giorni, since=args.da or "",
                                         until=args.a or "")
    corrected_ids = {c.get("id") for c in corrections}

    def group(key) -> dict:
        table: dict[str, dict] = {}
        for outcome in outcomes:
            name = str(key(outcome) or "—")
            row = table.setdefault(name, {"totale": 0, "corretti": 0})
            row["totale"] += 1
            if outcome.id in corrected_ids:
                row["corretti"] += 1
        for row in table.values():
            row["quota"] = (round(row["corretti"] / row["totale"], 3)
                            if row["totale"] else 0.0)
        return dict(sorted(table.items(), key=lambda kv: -kv[1]["corretti"]))

    report = {
        "esiti": len(outcomes),
        "correzioni": len(corrections),
        "quota_correzioni": (round(len(corrected_ids) / len(outcomes), 3)
                             if outcomes else 0.0),
        "per_confidenza": group(lambda o: o.confidence),
        "per_classe": group(lambda o: o.klass),
        "per_tipo_documento": group(lambda o: o.doc_type),
        "per_origine_instradamento": group(lambda o: o.route_origin),
        "per_cliente": group(lambda o: o.client),
        "per_mittente": dict(list(group(lambda o: o.sender).items())[:15]),
        "per_regola": group(lambda o: ", ".join(o.rules_applied)),
        "direttive_in_uso": sorted({
            f"{o.directives.get('versione', '?')} "
            f"({o.directives.get('regole', '?')})" for o in outcomes
        }),
    }
    if args.json:
        _out(json.dumps(report, ensure_ascii=False, indent=1))
        return EXIT_OK

    _out(f"esiti: {report['esiti']}   correzioni: {report['correzioni']}   "
         f"quota: {report['quota_correzioni']:.1%}")
    for title, key in (("confidenza", "per_confidenza"),
                       ("classe", "per_classe"),
                       ("tipo di documento", "per_tipo_documento"),
                       ("origine dell'instradamento", "per_origine_instradamento"),
                       ("cliente", "per_cliente"),
                       ("regola", "per_regola")):
        table = report[key]
        if not table:
            continue
        _out(f"\n{title}")
        for name, row in table.items():
            _out(f"  {name:<34} {row['totale']:>4} esiti  "
                 f"{row['corretti']:>3} corretti  {row['quota']:.1%}")
    if report["direttive_in_uso"]:
        _out("\ndirettive in uso nel periodo:")
        for version in report["direttive_in_uso"]:
            _out(f"  {version}")
    return EXIT_OK


def cmd_check(args, cfg: Config, directives: Directives) -> int:
    problems: list[str] = []
    _out(json.dumps(redacted(cfg), ensure_ascii=False, indent=1))
    _out(f"\ndirettive: versione {directives.version}, "
         f"{len(directives.rules)} regole, "
         f"{len(directives.doc_types)} tipi, "
         f"{len(directives.recipients)} destinatari")
    _out(f"  regole      {directives.rules_digest}")
    _out(f"  istruzioni  {directives.instructions_digest} "
         f"({len(directives.instructions)} caratteri)")
    _out(f"  sempre al titolare: {', '.join(directives.always_owner_types)}")

    # `coda` serve in scrittura come le altre: uscire dalla coda è un `rename`, e
    # spostare una cartella richiede il permesso sulla directory che la contiene.
    # È il controllo che avrebbe intercettato prima la coda montata in sola
    # lettura, invece di scoprirlo a spostamento fallito.
    for label, path, perche in (
        ("coda", cfg.queue_dir, "serve il rename fuori dalla coda"),
        ("esiti", cfg.outcomes_dir, "ci si scrivono gli esiti"),
        ("lavorati", cfg.worked_dir, "ci finisce il lavorato"),
        ("stato", cfg.state_dir, "ci sta lo stato locale"),
    ):
        exists = path.exists()
        ok = exists and os.access(path, os.W_OK)
        _out(f"  {label:<10} {path}  {'ok' if ok else 'MANCANTE o non scrivibile'}")
        if not exists:
            problems.append(f"{label}: {path} non esiste")
        elif not ok:
            problems.append(
                f"{label}: {path} non scrivibile ({perche}; la cartella deve "
                f"comparire fra i ReadWritePaths dell'unit systemd)")

    # Spazio: pecdesk scrive meno di pecfetch, ma scrive nello stesso albero, e
    # un disco pieno gli impedisce di registrare gli esiti già pagati al modello.
    ok_spazio, motivo = spazio.sufficiente(
        (cfg.outcomes_dir, cfg.worked_dir, cfg.state_dir), cfg.min_free_bytes)
    if ok_spazio:
        _out(f"  spazio     {spazio.leggibile(spazio.liberi(cfg.output_root))} liberi")
    else:
        _out(f"  spazio     SOTTO SOGLIA - {motivo}")
        problems.append(f"spazio insufficiente: {motivo}")

    try:
        from pecfetch.archive import Archive

        with Archive(cfg.archive_path, read_only=True) as archive:
            stats = archive.stats()
        _out(f"  archivio   {stats['totale']} messaggi, dal {stats['dal']} "
             f"al {stats['al']} (sola lettura)")
    except Exception as exc:                                   # noqa: BLE001
        _out(f"  archivio   non disponibile: {exc}")
        problems.append("archivio storico non leggibile: niente segnale relazionale")

    items, queue_problems = scan(cfg.queue_dir)
    _out(f"  in coda    {len(items)} messaggi")
    problems += queue_problems

    if args.api:
        try:
            classifier = make_classifier(cfg)
            probe = build_request(
                items[0], collect(items[0], History(), ""),
                build_material(items[0], cfg.material, directives.keywords),
                directives, cfg.model, cfg.max_tokens,
            ) if items else None
            if probe is not None:
                _out(f"  API        ok, {classifier.count_tokens(probe)} token "
                     f"per il primo messaggio in coda")
            else:
                _out("  API        client costruito (coda vuota: niente da contare)")
        except Exception as exc:                               # noqa: BLE001
            _out(f"  API        {exc}")
            problems.append(f"API: {exc}")

    if args.smtp:
        smtp_problems = smtp_check(cfg.digest)
        for problem in smtp_problems:
            _out(f"  SMTP       {problem}")
        problems += [p for p in smtp_problems if "disattivato" not in p]
    _out("")
    if problems:
        _out(f"{len(problems)} problemi:")
        for problem in problems:
            _out(f"  - {problem}")
        return EXIT_PARTIAL
    _out("tutto a posto")
    return EXIT_OK


def cmd_state(args, cfg: Config, directives: Directives) -> int:
    with State(cfg.state_path, cfg.timezone, cfg.permissions) as state:
        stats = state.stats()
        _out(json.dumps(stats, ensure_ascii=False, indent=1))
        suspended = state.suspended()
        if suspended:
            _out(f"\nsospesi ({len(suspended)}), restano in coda finché non li "
                 f"sblocchi con `pecdesk riprova <id>`:")
            for row in suspended:
                _out(f"  {row['id']}  tentativi {row['tentativi']}  "
                     f"{row['ultimo_errore'] or ''}")
        _out("\nultime esecuzioni:")
        for row in state.last_runs(5):
            _out(f"  {row['started_at']}  classificati {row['classificati']}  "
                 f"falliti {row['falliti']}  {row['nota'] or ''}")
    return EXIT_OK


def cmd_retry(args, cfg: Config, directives: Directives) -> int:
    with State(cfg.state_path, cfg.timezone, cfg.permissions) as state:
        for msg_id in args.id:
            ok = state.retry(msg_id)
            _out(f"{msg_id}: {'sbloccato' if ok else 'sconosciuto'}")
    return EXIT_OK


COMMANDS = {
    "run": (cmd_run, True),
    "riepilogo": (cmd_digest, True),
    "spiega": (cmd_explain, False),
    "correggi": (cmd_correct, False),
    "registro": (cmd_register, False),
    "check": (cmd_check, False),
    "stato": (cmd_state, False),
    "riprova": (cmd_retry, False),
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg, directives = _load(args)
    except (ConfigError, DirectivesError) as exc:
        print(f"pecdesk: {exc}", file=sys.stderr)
        return EXIT_FATAL

    handler, needs_lock = COMMANDS[args.command]
    if not needs_lock:
        try:
            return handler(args, cfg, directives)
        except Exception as exc:                               # noqa: BLE001
            log.exception("errore fatale")
            print(f"pecdesk: {exc}", file=sys.stderr)
            return EXIT_FATAL

    try:
        with RunLock(cfg.lock_path, cfg.permissions):
            return handler(args, cfg, directives)
    except AlreadyRunning as exc:
        print(f"pecdesk: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except Exception as exc:                                   # noqa: BLE001
        log.exception("errore fatale")
        print(f"pecdesk: {exc}", file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
