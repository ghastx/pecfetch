"""Configurazione: TOML fuori dal codice, credenziali fuori dal repository."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import permessi as perm
from . import spazio, tempo


class ConfigError(Exception):
    """Configurazione assente, malformata o incoerente."""


@dataclass(frozen=True)
class Account:
    id: str
    address: str
    host: str
    label: str = ""
    client_id: str = ""
    port: int = 993
    username: str = ""
    folder: str = "INBOX"
    ssl: bool = True
    starttls: bool = False
    enabled: bool = True
    timeout: int = 60
    password: str = field(default="", repr=False)
    # provenienza della password, per diagnostica (mai il valore)
    password_source: str = ""
    initial_lookback_days: int | None = None

    @property
    def display(self) -> str:
        return self.label or self.address or self.id


@dataclass(frozen=True)
class Config:
    output_root: Path
    state_dir: Path
    accounts: tuple[Account, ...]
    archive_path: Path
    log_file: Path | None
    log_level: str = "INFO"
    #: fuso di **tutte** le date esposte, non solo dei nomi dei file
    timezone: str = tempo.DEFAULT_TZ
    #: permessi dell'albero prodotto: dichiarati, non ereditati dalla umask
    permissions: perm.Permessi = perm.Permessi()
    #: sotto questa soglia non si comincia a scrivere un messaggio
    min_free_bytes: int = spazio.DEFAULT_MIN_FREE_BYTES

    initial_lookback_days: int = 7
    resync_overlap_days: int = 2
    max_messages_per_run: int = 500
    max_message_bytes: int = 50 * 1024 * 1024
    connect_retries: int = 2

    body_max_chars: int = 200_000
    attachment_max_bytes: int = 25 * 1024 * 1024
    attachment_store_max_bytes: int = 50 * 1024 * 1024
    #: eseguibili e script non vengono mai scritti nella cartella di output
    block_active_types: bool = True
    active_types_extra: tuple[str, ...] = ()

    extraction_enabled: bool = True
    ocr_enabled: bool = True
    ocr_lang: str = "ita"
    ocr_max_pages: int = 20
    ocr_dpi: int = 300
    ocr_timeout: int = 300
    extract_timeout: int = 120
    extract_max_chars: int = 400_000
    p7m_unwrap: bool = True

    #: limiti espliciti sull'apertura degli archivi
    archive_enabled: bool = True
    archive_max_ratio: int = 120
    archive_max_total_bytes: int = 64 * 1024 * 1024
    archive_max_entries: int = 500
    archive_max_depth: int = 3
    archive_max_member_bytes: int = 32 * 1024 * 1024

    source_path: Path | None = None

    @property
    def tz(self):
        """Il fuso già risolto. Validato al caricamento, qui non può fallire."""
        return tempo.zona(self.timezone)

    def account_by_id(self, account_id: str) -> Account | None:
        wanted = (account_id or "").strip().lower()
        for acc in self.accounts:
            if acc.id.lower() == wanted:
                return acc
        return None


DEFAULT_PATHS = (
    Path("/etc/pecfetch/pecfetch.toml"),
    Path("config/pecfetch.toml"),
)


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"file di configurazione non trovato: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML non valido in {path}: {exc}") from exc
    except OSError as exc:
        # permessi sbagliati, percorso che è una directory, disco che non
        # risponde: è un problema di configurazione e va detto come tale, non
        # lasciato risalire come traccia di stack da un'esecuzione notturna
        raise ConfigError(
            f"file di configurazione non leggibile: {path} "
            f"({exc.strerror or exc})"
        ) from exc


def _warn_if_world_readable(path: Path, warn) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        warn(f"{path} è leggibile da altri utenti: consigliato chmod 600")


def _resolve_password(raw: dict, secrets: dict, account_id: str, warn) -> tuple[str, str]:
    """Ritorna (password, provenienza). Il valore non viene mai loggato."""
    if "password_env" in raw:
        env = str(raw["password_env"])
        value = os.environ.get(env, "")
        if not value:
            raise ConfigError(
                f"casella '{account_id}': variabile d'ambiente {env} non valorizzata"
            )
        return value, f"env:{env}"
    if "password_file" in raw:
        path = Path(str(raw["password_file"])).expanduser()
        _warn_if_world_readable(path, warn)
        try:
            return path.read_text(encoding="utf-8").strip(), f"file:{path}"
        except OSError as exc:
            raise ConfigError(f"casella '{account_id}': {exc}") from exc
    passwords = secrets.get("passwords", {})
    key = str(raw.get("password_ref") or account_id)
    if key in passwords:
        return str(passwords[key]), "secrets"
    if "password" in raw:
        warn(
            f"casella '{account_id}': password in chiaro nel file principale; "
            "spostala nel file dei segreti o in una variabile d'ambiente"
        )
        return str(raw["password"]), "inline"
    raise ConfigError(
        f"casella '{account_id}': nessuna password (usa password_env, "
        "password_file, oppure una voce in [passwords] del file dei segreti)"
    )


def _account_from_raw(raw: dict, secrets: dict, defaults: dict, warn) -> Account:
    account_id = str(raw.get("id") or "").strip()
    # Nessun gestore PEC italiano tratta le caselle come sensibili alle
    # maiuscole: l'indirizzo è un dato del contratto e si normalizza subito, così
    # non c'è un solo confronto a valle che possa mancare per un LASERMARC@PEC.IT.
    # La forma scritta resta però quella che si manda al gestore per il login.
    address_scritto = str(raw.get("address") or "").strip()
    address = address_scritto.lower()
    if not account_id:
        account_id = address.split("@", 1)[0] if address else ""
    if not account_id:
        raise ConfigError("una casella è priva sia di 'id' che di 'address'")
    host = str(raw.get("host") or "").strip()
    if not host:
        raise ConfigError(f"casella '{account_id}': manca 'host'")
    if not address:
        raise ConfigError(f"casella '{account_id}': manca 'address'")

    ssl = bool(raw.get("ssl", True))
    starttls = bool(raw.get("starttls", False))
    port = int(raw.get("port", 143 if (starttls or not ssl) else 993))
    enabled = bool(raw.get("enabled", True))

    password, source = ("", "disabled")
    if enabled:
        password, source = _resolve_password(raw, secrets, account_id, warn)

    lookback = raw.get("initial_lookback_days")
    return Account(
        id=account_id,
        address=address,
        host=host,
        label=str(raw.get("label") or "").strip(),
        client_id=str(raw.get("client_id") or "").strip(),
        port=port,
        # lo username non si normalizza: è quello che si manda al gestore
        username=str(raw.get("username") or address_scritto).strip(),
        folder=str(raw.get("folder") or "INBOX"),
        ssl=ssl and not starttls,
        starttls=starttls,
        enabled=enabled,
        timeout=int(raw.get("timeout", defaults.get("timeout", 60))),
        password=password,
        password_source=source,
        initial_lookback_days=int(lookback) if lookback is not None else None,
    )


def load_config(path: str | os.PathLike | None = None, warn=None) -> Config:
    """Carica la configurazione. `warn` riceve gli avvisi non fatali."""
    warn = warn or (lambda msg: None)

    candidates = [Path(path)] if path else list(DEFAULT_PATHS)
    config_path = next((p for p in candidates if p.is_file()), None)
    if config_path is None:
        raise ConfigError(
            "nessun file di configurazione trovato (cercati: "
            + ", ".join(str(p) for p in candidates)
            + ")"
        )
    data = _load_toml(config_path)
    base = config_path.parent

    general = data.get("general", {})
    fetch = data.get("fetch", {})
    body = data.get("body", {})
    att = data.get("attachments", {})
    extr = data.get("extraction", {})
    archive = extr.get("archive", {})
    arch = data.get("archive", {})
    logs = data.get("logging", {})
    perms_raw = data.get("permissions", {})

    def as_path(value: str) -> Path:
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    if "output_root" not in general:
        raise ConfigError("manca [general].output_root")
    output_root = as_path(general["output_root"])
    state_dir = as_path(general.get("state_dir", "/var/lib/pecfetch"))

    secrets: dict = {}
    secrets_file = general.get("secrets_file")
    if secrets_file:
        secrets_path = as_path(secrets_file)
        # tre casi, non due: assente è tollerato (le password possono venire da
        # password_env), ma "c'è e non si legge" no. Trattarli allo stesso modo
        # farebbe fallire l'esecuzione molto più a valle, con un «nessuna
        # password» che non dice dov'è davvero il problema.
        if secrets_path.is_file():
            _warn_if_world_readable(secrets_path, warn)
            secrets = _load_toml(secrets_path)
        elif secrets_path.exists():
            raise ConfigError(
                f"file dei segreti non leggibile: {secrets_path} non è un file "
                f"regolare"
            )
        elif secrets_path.parent.exists() and not os.access(secrets_path.parent, os.X_OK):
            raise ConfigError(
                f"file dei segreti non leggibile: {secrets_path.parent} non è "
                f"attraversabile da questo utente"
            )
        else:
            warn(f"file dei segreti non trovato: {secrets_path}")

    raw_accounts = list(data.get("accounts", []))
    accounts_file = general.get("accounts_file")
    if accounts_file:
        acc_path = as_path(accounts_file)
        if not acc_path.is_file():
            raise ConfigError(f"accounts_file non trovato: {acc_path}")
        _warn_if_world_readable(acc_path, warn)
        acc_data = _load_toml(acc_path)
        raw_accounts += list(acc_data.get("accounts", []))
        if "passwords" in acc_data:
            secrets.setdefault("passwords", {}).update(acc_data["passwords"])

    if not raw_accounts:
        raise ConfigError("nessuna casella configurata")

    accounts: list[Account] = []
    seen: set[str] = set()
    seen_addr: dict[str, str] = {}
    for raw in raw_accounts:
        acc = _account_from_raw(raw, secrets, fetch, warn)
        if acc.id.lower() in seen:
            raise ConfigError(f"id casella duplicato: '{acc.id}'")
        seen.add(acc.id.lower())
        # due caselle sullo stesso indirizzo scritte con maiuscole diverse sono
        # la stessa casella scaricata due volte, con due cursori che si ignorano
        if acc.address in seen_addr:
            raise ConfigError(
                f"casella '{acc.id}': indirizzo {acc.address} già dichiarato "
                f"da '{seen_addr[acc.address]}'"
            )
        seen_addr[acc.address] = acc.id
        accounts.append(acc)

    archive_path = (
        as_path(arch["path"]) if arch.get("path") else state_dir / "archivio.sqlite3"
    )
    log_file = as_path(logs["file"]) if logs.get("file") else None

    # Il fuso governa tutte le date esposte: un refuso qui non deve diventare un
    # silenzioso ripiego su UTC, che è esattamente la mescolanza da evitare.
    timezone_name = str(general.get("timezone", tempo.DEFAULT_TZ))
    try:
        tempo.zona(timezone_name)
    except ValueError as exc:
        raise ConfigError(f"[general].timezone: {exc}") from exc

    try:
        permissions = perm.Permessi(
            dir_mode=perm.modo(perms_raw.get("dir_mode"), perm.DIR_MODE),
            file_mode=perm.modo(perms_raw.get("file_mode"), perm.FILE_MODE),
            shared_dir_mode=perm.modo(perms_raw.get("shared_dir_mode"),
                                      perm.SHARED_DIR_MODE),
            group=str(perms_raw.get("group", "")).strip(),
        )
    except ValueError as exc:
        raise ConfigError(f"[permissions]: {exc}") from exc
    if permissions.group and permissions.gid < 0:
        warn(f"[permissions].group = '{permissions.group}' non esiste su questa "
             "macchina: i file resteranno nel gruppo del processo")

    return Config(
        output_root=output_root,
        state_dir=state_dir,
        accounts=tuple(accounts),
        archive_path=archive_path,
        log_file=log_file,
        log_level=str(logs.get("level", "INFO")).upper(),
        timezone=timezone_name,
        permissions=permissions,
        min_free_bytes=int(general.get("min_free_bytes",
                                       spazio.DEFAULT_MIN_FREE_BYTES)),
        initial_lookback_days=int(fetch.get("initial_lookback_days", 7)),
        resync_overlap_days=int(fetch.get("resync_overlap_days", 2)),
        max_messages_per_run=int(fetch.get("max_messages_per_run", 500)),
        max_message_bytes=int(fetch.get("max_message_bytes", 50 * 1024 * 1024)),
        connect_retries=int(fetch.get("connect_retries", 2)),
        body_max_chars=int(body.get("max_chars", 200_000)),
        attachment_max_bytes=int(att.get("max_extract_bytes", 25 * 1024 * 1024)),
        attachment_store_max_bytes=int(att.get("max_store_bytes", 50 * 1024 * 1024)),
        block_active_types=bool(att.get("block_active_types", True)),
        active_types_extra=tuple(
            str(e).lstrip(".").lower() for e in att.get("active_types_extra", [])
        ),
        extraction_enabled=bool(extr.get("enabled", True)),
        ocr_enabled=bool(extr.get("ocr", True)),
        ocr_lang=str(extr.get("ocr_lang", "ita")),
        ocr_max_pages=int(extr.get("ocr_max_pages", 20)),
        ocr_dpi=int(extr.get("ocr_dpi", 300)),
        ocr_timeout=int(extr.get("ocr_timeout", 300)),
        extract_timeout=int(extr.get("timeout", 120)),
        extract_max_chars=int(extr.get("max_text_chars", 400_000)),
        p7m_unwrap=bool(extr.get("p7m_unwrap", True)),
        archive_enabled=bool(archive.get("enabled", True)),
        archive_max_ratio=int(archive.get("max_ratio", 120)),
        archive_max_total_bytes=int(archive.get("max_total_bytes", 64 * 1024 * 1024)),
        archive_max_entries=int(archive.get("max_entries", 500)),
        archive_max_depth=int(archive.get("max_depth", 3)),
        archive_max_member_bytes=int(archive.get("max_member_bytes", 32 * 1024 * 1024)),
        source_path=config_path,
    )


def redacted(cfg: Config) -> dict:
    """Vista della configurazione sicura da loggare."""
    return {
        "config": str(cfg.source_path),
        "output_root": str(cfg.output_root),
        "state_dir": str(cfg.state_dir),
        "archive": str(cfg.archive_path),
        "timezone": cfg.timezone,
        "permissions": perm.descrizione(cfg.permissions),
        "min_free_bytes": cfg.min_free_bytes,
        "accounts": [
            {
                "id": a.id,
                "address": a.address,
                "host": f"{a.host}:{a.port}",
                "folder": a.folder,
                "enabled": a.enabled,
                "password": f"<{a.password_source}>",
            }
            for a in cfg.accounts
        ],
    }


__all__ = ["Account", "Config", "ConfigError", "load_config", "redacted", "replace"]
