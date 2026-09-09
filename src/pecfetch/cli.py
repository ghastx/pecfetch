"""Interfaccia a riga di comando.

Codici di uscita (l'esecuzione è periodica e non presidiata, quindi contano):

    0  tutto bene
    2  completato, ma con caselle in errore
    1  errore fatale (configurazione, stato, output)
    3  un'altra esecuzione era già in corso
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__, permessi as perm, spazio
from .archive import Archive
from .config import Config, ConfigError, load_config, redacted
from .extract import ExtractorSettings, available_tools
from .imapclient import ImapError
from .lock import AlreadyRunning, RunLock
from .logging_setup import setup as setup_logging
from .output import OutputWriter
from .pipeline import (
    MODE_BACKFILL,
    MODE_INIT,
    MODE_RUN,
    PROVA,
    Pipeline,
    RunSummary,
    default_connect,
)
from .state import State

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2
EXIT_LOCKED = 3

log = logging.getLogger("pecfetch")


# ---------------------------------------------------------------------------
# argomenti
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pecfetch",
        description="Estrae in sola lettura i messaggi dalle caselle PEC e li "
                    "scrive come dati strutturati nella cartella di output.",
    )
    parser.add_argument("--version", action="version", version=f"pecfetch {__version__}")
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="file di configurazione TOML")
    parser.add_argument("-v", "--verbose", action="store_true", help="log di debug")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="su console solo avvisi ed errori")
    # Le stesse opzioni globali accettate anche dopo il sottocomando: chi
    # amministra la VM scrive `pecfetch run -c ...` e deve funzionare.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="FILE",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    _sub = parser.add_subparsers(dest="command", required=True)

    class sub:  # sottile: aggiunge `common` a ogni sottocomando
        @staticmethod
        def add_parser(name, **kwargs):
            kwargs.setdefault("parents", []).append(common)
            return _sub.add_parser(name, **kwargs)

    def add_account_filter(sp):
        sp.add_argument("-a", "--account", action="append", default=[],
                        metavar="ID", help="limita alle caselle indicate (ripetibile)")

    p_run = sub.add_parser("run", help="scarica i messaggi nuovi (uso normale)")
    add_account_filter(p_run)
    p_run.add_argument("--limit", type=int, help="massimo messaggi per casella")
    p_run.add_argument("--dry-run", action="store_true",
                       help="mostra cosa scaricherebbe senza scrivere nulla")

    p_init = sub.add_parser(
        "init",
        help="fissa la posizione di partenza senza scaricare nulla",
    )
    add_account_filter(p_init)
    p_init.add_argument("--lookback-days", type=int, metavar="N",
                        help="parti da N giorni fa invece che da adesso")
    p_init.add_argument("--dry-run", action="store_true")

    p_back = sub.add_parser("backfill", help="scarica l'archivio storico")
    add_account_filter(p_back)
    p_back.add_argument("--since", required=True, metavar="AAAA-MM-GG",
                        help="data di partenza")
    p_back.add_argument("--limit", type=int, help="massimo messaggi per casella")
    p_back.add_argument("--dry-run", action="store_true")

    p_status = sub.add_parser("status", help="stato delle caselle e ultime esecuzioni")
    p_status.add_argument("--json", action="store_true")

    p_check = sub.add_parser("check", help="verifica configurazione e strumenti")
    p_check.add_argument("--login", action="store_true",
                         help="prova anche il collegamento IMAP (sola lettura)")
    p_check.add_argument("--folders", action="store_true",
                         help="elenca le cartelle di ogni casella")

    p_search = sub.add_parser("search", help="cerca nell'archivio storico")
    p_search.add_argument("query", nargs="*", help="testo da cercare")
    p_search.add_argument("--client", help="cliente o id casella")
    p_search.add_argument("--from", dest="sender", help="mittente o dominio")
    p_search.add_argument("--since", help="AAAA-MM-GG")
    p_search.add_argument("--until", help="AAAA-MM-GG")
    p_search.add_argument("--type", dest="msg_type", help="tipo di messaggio")
    p_search.add_argument("--limit", type=int, default=30)
    p_search.add_argument("--json", action="store_true")

    p_receipts = sub.add_parser(
        "receipts", help="ricevute positive registrate (non finiscono in output)"
    )
    p_receipts.add_argument("--limit", type=int, default=50)
    p_receipts.add_argument("--account")

    p_parse = sub.add_parser(
        "parse", help="analizza un .eml locale (diagnostica, nessun IMAP)"
    )
    p_parse.add_argument("file", type=Path)
    p_parse.add_argument("--json", action="store_true")

    p_clean = sub.add_parser(
        "cleanup", help="rimuove i montaggi rimasti da run interrotti")
    p_clean.add_argument("--permessi", action="store_true",
                         help="riapplica il modello di permessi all'albero prodotto")
    return parser


# ---------------------------------------------------------------------------
# comandi
# ---------------------------------------------------------------------------

def _select_accounts(cfg: Config, wanted: list[str]):
    if not wanted:
        return list(cfg.accounts)
    out = []
    for account_id in wanted:
        account = cfg.account_by_id(account_id)
        if account is None:
            raise ConfigError(f"casella sconosciuta: '{account_id}'")
        out.append(account)
    return out


def _open_stack(cfg: Config, layout: bool = True):
    state = State(cfg.state_dir / "stato.sqlite3", cfg.timezone, cfg.permissions)
    archive = Archive(cfg.archive_path, permissions=cfg.permissions)
    writer = OutputWriter(
        cfg.output_root,
        ExtractorSettings.from_config(cfg),
        body_max_chars=cfg.body_max_chars,
        attachment_store_max_bytes=cfg.attachment_store_max_bytes,
        timezone=cfg.timezone,
        permissions=cfg.permissions,
        layout=layout,
    )
    return state, archive, writer


def cmd_fetch(cfg: Config, args, mode: str) -> int:
    accounts = _select_accounts(cfg, args.account)
    since = None
    if mode == MODE_BACKFILL:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            raise ConfigError(f"data non valida: {args.since!r} (attesa AAAA-MM-GG)")

    lock = RunLock(cfg.state_dir / "pecfetch.lock", cfg.permissions)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        log.warning("%s: non faccio nulla", exc)
        return EXIT_LOCKED

    dry_run = bool(getattr(args, "dry_run", False))
    state = archive = None
    try:
        # In prova a vuoto l'albero di output non si crea e i montaggi
        # incompleti non si cancellano: sono scritture, e una prova a vuoto non
        # ne fa nessuna. Lo stato locale si apre lo stesso, in sola lettura di
        # fatto: serve a sapere da dove si ripartirebbe.
        state, archive, writer = _open_stack(cfg, layout=not dry_run)
        if not dry_run:
            removed = writer.cleanup_staging()
            if removed:
                log.info("rimossi %d montaggi incompleti di esecuzioni precedenti",
                         removed)

        run_id = None if dry_run else state.start_run(mode)
        pipeline = Pipeline(cfg, state, writer, archive, default_connect, run_id)
        summary = pipeline.run(
            mode=mode,
            accounts=accounts,
            since=since,
            lookback_days=getattr(args, "lookback_days", None),
            limit=getattr(args, "limit", None),
            dry_run=dry_run,
        )
        exit_code = EXIT_PARTIAL if summary.accounts_err else EXIT_OK
        _print_summary(summary, dry_run)
        if run_id is not None:
            state.finish_run(run_id, summary.accounts_ok, summary.accounts_err,
                             summary.written, summary.receipts, exit_code)
        return exit_code
    finally:
        if archive:
            archive.close()
        if state:
            state.close()
        lock.release()


def _print_summary(summary: RunSummary, dry_run: bool) -> None:
    prefix = PROVA if dry_run else ""
    log.info(
        "%sriepilogo %s: %d caselle ok, %d in errore, %d messaggi %s, "
        "%d ricevute positive registrate, %d duplicati scartati, %d rimasti in coda",
        prefix, summary.mode, summary.accounts_ok, summary.accounts_err,
        summary.candidates if dry_run else summary.written,
        "da scaricare" if dry_run else "scritti",
        summary.receipts, summary.duplicates, summary.remaining,
    )
    for account_id, error in summary.errors:
        log.error("%scasella in errore: %s -> %s", prefix, account_id, error)
    if summary.remaining:
        log.info("%s%d messaggi non ancora scaricati: verranno presi alla prossima "
                 "esecuzione", prefix, summary.remaining)


def cmd_status(cfg: Config, args) -> int:
    with State(cfg.state_dir / "stato.sqlite3", cfg.timezone,
               cfg.permissions) as state, \
            Archive(cfg.archive_path, permissions=cfg.permissions) as archive:
        payload = {
            "configurazione": redacted(cfg),
            "stato": state.stats(),
            "archivio": archive.stats(),
            "caselle": [dict(row) for row in state.mailbox_rows()],
            "ultime_esecuzioni": [dict(row) for row in state.last_runs(10)],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return EXIT_OK

        print(f"pecfetch {__version__}  ({cfg.source_path})")
        print(f"  output   : {cfg.output_root}")
        print(f"  stato    : {cfg.state_dir}")
        print(f"  archivio : {cfg.archive_path}")
        stats = payload["stato"]
        print(f"  messaggi : {stats['messages_done']} completati, "
              f"{stats['messages_pending']} in sospeso, "
              f"{stats['messages_failed']} falliti, "
              f"{stats['receipts']} ricevute positive")
        arch = payload["archivio"]
        print(f"  archivio : {arch['totale']} record  {arch.get('dal') or '-'} .. "
              f"{arch.get('al') or '-'}")
        print()
        header = f"{'casella':<20} {'uidval':>10} {'ultimo uid':>10} {'ultimo ok':<20} esito"
        print(header)
        print("-" * len(header))
        known = {row["account"]: row for row in state.mailbox_rows()}
        for account in cfg.accounts:
            row = known.get(account.id)
            if row is None:
                print(f"{account.id:<20} {'-':>10} {'-':>10} {'mai':<20} "
                      f"{'da inizializzare' if account.enabled else 'disabilitata'}")
                continue
            esito = "ok" if not row["last_error"] else f"ERRORE: {row['last_error'][:60]}"
            print(f"{account.id:<20} {row['uidvalidity'] or '-':>10} "
                  f"{row['last_uid']:>10} {(row['last_ok_at'] or 'mai')[:19]:<20} {esito}")
        print()
        for row in state.last_runs(5):
            print(f"  run #{row['id']} {row['mode']:<9} {(row['started_at'] or '')[:19]} "
                  f"-> ok={row['accounts_ok']} err={row['accounts_err']} "
                  f"scritti={row['written']} exit={row['exit_code']}")
    return EXIT_OK


def cmd_check(cfg: Config, args) -> int:
    problems = 0
    print(f"configurazione : {cfg.source_path}")
    print(f"state_dir      : {cfg.state_dir}")
    print(f"archivio       : {cfg.archive_path}")
    print(f"caselle        : {len(cfg.accounts)} "
          f"({sum(1 for a in cfg.accounts if a.enabled)} attive)")
    print(f"fuso orario    : {cfg.timezone}  (tutte le date esposte, non solo i nomi)")
    print(f"permessi       : {perm.descrizione(cfg.permissions)}")

    # `check` non deve avere effetti collaterali: qui si guarda e basta.
    if not cfg.output_root.is_dir():
        print(f"output_root    : {cfg.output_root}  NON ESISTE")
        problems += 1
    elif not os.access(cfg.output_root, os.W_OK | os.X_OK):
        print(f"output_root    : {cfg.output_root}  NON SCRIVIBILE")
        problems += 1
    else:
        print(f"output_root    : {cfg.output_root}  scrivibile")

    # Spazio: i limiti sugli allegati proteggono dal singolo file patologico,
    # non dall'accumulo di una coda che nessuno svuota.
    ok_spazio, motivo = spazio.sufficiente(
        (cfg.output_root, cfg.state_dir), cfg.min_free_bytes)
    liberi = spazio.leggibile(spazio.liberi(cfg.output_root))
    soglia = spazio.leggibile(cfg.min_free_bytes)
    if ok_spazio:
        print(f"spazio libero  : {liberi} (soglia {soglia})")
    else:
        print(f"spazio libero  : SOTTO SOGLIA - {motivo}")
        problems += 1

    # I permessi dell'albero già prodotto: divergono se è stato scritto da una
    # versione precedente, o se qualcuno ci ha messo le mani.
    if cfg.output_root.is_dir():
        writer = OutputWriter(cfg.output_root, ExtractorSettings.from_config(cfg),
                              timezone=cfg.timezone, permissions=cfg.permissions,
                              layout=False)
        fuori = writer.divergenze()
        if fuori:
            print(f"permessi albero: {len(fuori)} percorsi fuori modello "
                  f"(sistemabili con 'pecfetch cleanup --permessi')")
            for riga in fuori[:5]:
                print(f"  {riga}")
            problems += 1

    print("\ntrattamento degli allegati:")
    print(f"  tipi attivi        {'mai materializzati' if cfg.block_active_types else 'CONSENTITI'}"
          + (f"  (+{len(cfg.active_types_extra)} estensioni aggiuntive)"
             if cfg.active_types_extra else ""))
    if not cfg.block_active_types:
        print("  ATTENZIONE: eseguibili e script verranno scritti in output")
        problems += 1
    if cfg.archive_enabled:
        print(f"  archivi            rapporto max {cfg.archive_max_ratio}:1, "
              f"{cfg.archive_max_total_bytes // (1024 * 1024)} MiB espansi, "
              f"{cfg.archive_max_entries} voci, {cfg.archive_max_depth} livelli, "
              f"{cfg.archive_max_member_bytes // (1024 * 1024)} MiB per voce")
    else:
        print("  archivi            non aperti (estrazione disabilitata)")
    print("  formati opachi     rar, 7z e immagini disco non vengono aperti")

    tools = available_tools()
    print("\nstrumenti di estrazione:")
    for name in ("pdftotext", "pypdf", "pdfminer", "pdftoppm", "tesseract", "openssl"):
        state_txt = "presente" if tools.get(name) else "ASSENTE"
        print(f"  {name:<12} {state_txt}")
    langs = tools.get("tesseract_langs") or []
    if tools.get("tesseract"):
        elenco = ", ".join(langs) if langs else "?"
        print(f"  lingue OCR   ({len(langs)}) {elenco}"
              + ("" if cfg.ocr_lang in langs else f"   <-- '{cfg.ocr_lang}' NON installata"))
        if cfg.ocr_lang not in langs:
            problems += 1
    if not tools.get("pdftotext") and not tools.get("pypdf") and not tools.get("pdfminer"):
        print("  ATTENZIONE: nessuno strumento per il testo dei PDF")
        problems += 1

    if args.login or args.folders:
        print("\ncollegamenti (sola lettura):")
        for account in cfg.accounts:
            if not account.enabled:
                print(f"  {account.id:<20} disabilitata")
                continue
            reader = default_connect(account)
            try:
                reader.connect()
                uidvalidity = reader.examine(account.folder)
                print(f"  {account.id:<20} ok  {account.host}  "
                      f"{account.folder}: {reader.exists} messaggi, "
                      f"uidvalidity={uidvalidity}")
                if args.folders:
                    for folder in reader.list_folders():
                        print(f"      {folder}")
            except ImapError as exc:
                print(f"  {account.id:<20} ERRORE {exc}")
                problems += 1
            finally:
                reader.close()

    if not problems:
        print("\nnessun problema")
    elif problems == 1:
        print("\n1 problema rilevato")
    else:
        print(f"\n{problems} problemi rilevati")
    return EXIT_OK if not problems else EXIT_PARTIAL


def cmd_search(cfg: Config, args) -> int:
    with Archive(cfg.archive_path, permissions=cfg.permissions) as archive:
        hits = archive.search(
            query=" ".join(args.query),
            client=args.client or "",
            sender=args.sender or "",
            since=args.since or "",
            until=args.until or "",
            msg_type=args.msg_type or "",
            limit=args.limit,
        )
        if args.json:
            print(json.dumps([h.__dict__ for h in hits], ensure_ascii=False, indent=2))
            return EXIT_OK
        if not hits:
            print("nessun risultato")
            return EXIT_OK
        for hit in hits:
            print(f"{hit.date:<19} {hit.client:<16} {hit.msg_type:<20} "
                  f"{hit.from_addr:<34} {hit.subject[:60]}")
            print(f"{'':19} {hit.content_dir}")
            if hit.snippet:
                print(f"{'':19} … {hit.snippet.strip()}")
        print(f"\n{len(hits)} risultati")
    return EXIT_OK


def cmd_receipts(cfg: Config, args) -> int:
    with State(cfg.state_dir / "stato.sqlite3", cfg.timezone,
               cfg.permissions) as state:
        sql = "SELECT * FROM receipts"
        params: list = []
        if args.account:
            sql += " WHERE account=?"
            params.append(args.account)
        sql += " ORDER BY COALESCE(date_certified, recorded_at) DESC LIMIT ?"
        params.append(args.limit)
        rows = list(state.db.execute(sql, params))
        if not rows:
            print("nessuna ricevuta registrata")
            return EXIT_OK
        for row in rows:
            print(f"{(row['date_certified'] or row['recorded_at'])[:19]:<19} "
                  f"{row['account']:<16} {row['kind']:<20} "
                  f"{(row['subject'] or '')[:60]}")
        print(f"\n{len(rows)} ricevute (non finiscono in output: sono rumore per il "
              "consumatore)")
    return EXIT_OK


def cmd_parse(cfg: Config | None, args) -> int:
    from .pec import parse_pec

    raw = args.file.read_bytes()
    pm = parse_pec(raw)
    if args.json:
        print(json.dumps({
            "tipo": pm.msg_type,
            "certificato": pm.certified,
            "ricevuta": pm.receipt_kind,
            "classe": pm.receipt_class,
            "oggetto": pm.subject,
            "mittente": pm.from_addr,
            "destinatari": pm.to,
            "message_id": pm.message_id,
            "riferimento": pm.ref_message_id,
            "gestore": pm.gestore,
            "identificativo": pm.pec_identifier,
            "allegati": [
                {"nome": a.filename, "content_type": a.content_type, "byte": a.size}
                for a in pm.attachments
            ],
            "note": pm.flags,
            "corpo_caratteri": len(pm.body_text),
        }, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    print(f"tipo           : {pm.msg_type} (certificato: {pm.certified})")
    if pm.receipt_kind:
        print(f"ricevuta       : {pm.receipt_kind} [{pm.receipt_class}] "
              f"rif. {pm.ref_message_id}")
    print(f"mittente reale : {pm.from_addr.get('address', '?')}")
    print(f"destinatari    : {', '.join(a.get('address', '') for a in pm.to)}")
    print(f"oggetto        : {pm.subject}")
    print(f"data certif.   : {pm.date_certified}")
    print(f"gestore        : {pm.gestore}  id: {pm.pec_identifier}")
    print(f"allegati       : {len(pm.attachments)}")
    for att in pm.attachments:
        print(f"   - {att.filename}  {att.content_type}  {att.size} byte")
    if pm.flags:
        print(f"note           : {', '.join(pm.flags)}")
    print(f"\n--- corpo ({len(pm.body_text)} caratteri, origine {pm.body_source}) ---")
    print(pm.body_text[:2000])
    return EXIT_OK


def cmd_cleanup(cfg: Config, args) -> int:
    writer = OutputWriter(cfg.output_root, ExtractorSettings.from_config(cfg),
                          timezone=cfg.timezone, permissions=cfg.permissions)
    if getattr(args, "permessi", False):
        # Le cartelle scritte prima che il modello fosse dichiarato hanno i
        # permessi che capitavano: qui si riportano tutte a quello scelto.
        fuori = writer.applica_permessi()
        print(f"permessi riapplicati a {cfg.output_root}: "
              f"{perm.descrizione(cfg.permissions)}")
        if fuori:
            print(f"ATTENZIONE: {len(fuori)} percorsi restano fuori modello")
            for riga in fuori[:5]:
                print(f"  {riga}")
            return EXIT_PARTIAL
        return EXIT_OK
    removed = writer.cleanup_staging(max_age_seconds=3600)
    print(f"{removed} montaggi incompleti rimossi")
    return EXIT_OK


# ---------------------------------------------------------------------------
# ingresso
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.config = getattr(args, "config", None)
    args.verbose = getattr(args, "verbose", False)
    args.quiet = getattr(args, "quiet", False)

    # 'parse' è diagnostica pura: deve funzionare anche senza configurazione.
    if args.command == "parse":
        setup_logging("INFO", None, args.quiet, args.verbose)
        try:
            return cmd_parse(None, args)
        except OSError as exc:
            print(f"errore: {exc}", file=sys.stderr)
            return EXIT_FATAL

    warnings: list[str] = []
    try:
        cfg = load_config(args.config, warn=warnings.append)
    except ConfigError as exc:
        setup_logging("INFO", None, args.quiet, args.verbose)
        log.error("configurazione: %s", exc)
        return EXIT_FATAL

    # Rete di sicurezza per ciò che dovesse sfuggire ai chmod espliciti: da qui
    # in poi la umask non può concedere più di quanto il modello preveda.
    os.umask(perm.umask_da(cfg.permissions))
    setup_logging(cfg.log_level, cfg.log_file, args.quiet, args.verbose,
                  timezone_name=cfg.timezone)
    for message in warnings:
        log.warning("configurazione: %s", message)

    try:
        if args.command == "run":
            return cmd_fetch(cfg, args, MODE_RUN)
        if args.command == "init":
            return cmd_fetch(cfg, args, MODE_INIT)
        if args.command == "backfill":
            return cmd_fetch(cfg, args, MODE_BACKFILL)
        if args.command == "status":
            return cmd_status(cfg, args)
        if args.command == "check":
            return cmd_check(cfg, args)
        if args.command == "search":
            return cmd_search(cfg, args)
        if args.command == "receipts":
            return cmd_receipts(cfg, args)
        if args.command == "cleanup":
            return cmd_cleanup(cfg, args)
    except ConfigError as exc:
        log.error("configurazione: %s", exc)
        return EXIT_FATAL
    except KeyboardInterrupt:
        log.warning("interrotto dall'utente")
        return EXIT_FATAL
    except Exception as exc:
        log.exception("errore fatale: %s", exc)
        return EXIT_FATAL

    log.error("comando sconosciuto: %s", args.command)
    return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
