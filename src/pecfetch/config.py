"""Configurazione: TOML fuori dal codice, credenziali fuori dal repository."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


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
    timezone: str = "Europe/Rome"

    initial_lookback_days: int = 7
    resync_overlap_days: int = 2
    max_messages_per_run: int = 500
    max_message_bytes: int = 50 * 1024 * 1024
    connect_retries: int = 2

    body_max_chars: int = 200_000
    attachment_max_bytes: int = 25 * 1024 * 1024
    attachment_store_max_bytes: int = 50 * 1024 * 1024

    extraction_enabled: bool = True
    ocr_enabled: bool = True
    ocr_lang: str = "ita"
    ocr_max_pages: int = 20
    ocr_dpi: int = 300
    ocr_timeout: int = 300
    extract_timeout: int = 120
    extract_max_chars: int = 400_000
    p7m_unwrap: bool = True

    source_path: Path | None = None

    def account_by_id(self, account_id: str) -> Account | None:
        for acc in self.accounts:
            if acc.id == account_id:
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
    address = str(raw.get("address") or "").strip()
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
        username=str(raw.get("username") or address).strip(),
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
    arch = data.get("archive", {})
    logs = data.get("logging", {})

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
        if secrets_path.is_file():
            _warn_if_world_readable(secrets_path, warn)
            secrets = _load_toml(secrets_path)
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
    for raw in raw_accounts:
        acc = _account_from_raw(raw, secrets, fetch, warn)
        if acc.id in seen:
            raise ConfigError(f"id casella duplicato: '{acc.id}'")
        seen.add(acc.id)
        accounts.append(acc)

    archive_path = (
        as_path(arch["path"]) if arch.get("path") else state_dir / "archivio.sqlite3"
    )
    log_file = as_path(logs["file"]) if logs.get("file") else None

    return Config(
        output_root=output_root,
        state_dir=state_dir,
        accounts=tuple(accounts),
        archive_path=archive_path,
        log_file=log_file,
        log_level=str(logs.get("level", "INFO")).upper(),
        timezone=str(general.get("timezone", "Europe/Rome")),
        initial_lookback_days=int(fetch.get("initial_lookback_days", 7)),
        resync_overlap_days=int(fetch.get("resync_overlap_days", 2)),
        max_messages_per_run=int(fetch.get("max_messages_per_run", 500)),
        max_message_bytes=int(fetch.get("max_message_bytes", 50 * 1024 * 1024)),
        connect_retries=int(fetch.get("connect_retries", 2)),
        body_max_chars=int(body.get("max_chars", 200_000)),
        attachment_max_bytes=int(att.get("max_extract_bytes", 25 * 1024 * 1024)),
        attachment_store_max_bytes=int(att.get("max_store_bytes", 50 * 1024 * 1024)),
        extraction_enabled=bool(extr.get("enabled", True)),
        ocr_enabled=bool(extr.get("ocr", True)),
        ocr_lang=str(extr.get("ocr_lang", "ita")),
        ocr_max_pages=int(extr.get("ocr_max_pages", 20)),
        ocr_dpi=int(extr.get("ocr_dpi", 300)),
        ocr_timeout=int(extr.get("ocr_timeout", 300)),
        extract_timeout=int(extr.get("timeout", 120)),
        extract_max_chars=int(extr.get("max_text_chars", 400_000)),
        p7m_unwrap=bool(extr.get("p7m_unwrap", True)),
        source_path=config_path,
    )


def redacted(cfg: Config) -> dict:
    """Vista della configurazione sicura da loggare."""
    return {
        "config": str(cfg.source_path),
        "output_root": str(cfg.output_root),
        "state_dir": str(cfg.state_dir),
        "archive": str(cfg.archive_path),
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
