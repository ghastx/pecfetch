"""Configurazione di pecdesk: TOML fuori dal codice, chiave API fuori dal repository.

Stesse convenzioni di ``pecfetch.config`` — un file TOML, percorsi relativi
risolti rispetto al file, avvisi non fatali su un callback — ma un file suo, uno
stato suo e una modalità di guasto sua. Sono due programmi, non due modalità
dello stesso programma.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pecfetch import permessi as perm, tempo


class ConfigError(Exception):
    """Configurazione assente, malformata o incoerente."""


#: Il modello predefinito. L'obiettivo è lo smistamento, non l'analisi: il
#: compito è alla portata di un modello economico, e via API Batch costa la metà.
DEFAULT_MODEL = "claude-haiku-4-5"


@dataclass(frozen=True)
class MaterialLimits:
    """Quanto materiale si manda al modello. Ogni taglio viene dichiarato."""

    #: caratteri del corpo del messaggio
    body_chars: int = 1_500
    #: caratteri per singolo allegato (testa + finestre sulle parole chiave)
    attachment_chars: int = 1_200
    #: caratteri della sola "testa" di un allegato
    attachment_head_chars: int = 600
    #: quante finestre attorno alle parole chiave, e quanto larghe
    keyword_windows: int = 4
    keyword_window_chars: int = 240
    #: quanti allegati portano testo; gli altri restano solo nell'inventario
    max_attachments: int = 3
    #: tetto complessivo sul materiale non fidato
    total_chars: int = 4_000


@dataclass(frozen=True)
class SuspicionLimits:
    """Soglie del sospetto. Il segnale relazionale ha una partenza a freddo."""

    #: sotto questa copertura dell'archivio, "mittente mai visto" non basta
    min_history_days: int = 90
    #: ...né sotto questo numero di messaggi già visti sulla casella
    min_history_messages: int = 20


@dataclass(frozen=True)
class ApiSettings:
    """Chiamate sincrone, una per messaggio: isolamento dei guasti e ripresa
    per singolo elemento. Un messaggio che fallisce non trascina gli altri."""

    #: ritentativi dentro l'SDK per errori transitori (429, 5xx, rete)
    max_retries: int = 3
    timeout_seconds: float = 60.0
    #: tetto di messaggi per esecuzione, per non lavorare un anno di arretrato
    #: tutto in una notte
    max_messages: int = 200
    #: dopo N fallimenti di fila si smette: è un guasto sistemico, non un caso
    stop_after_failures: int = 5
    #: pausa fra una chiamata e l'altra, se si volesse essere gentili
    pause_seconds: float = 0.0


@dataclass(frozen=True)
class DigestSettings:
    """Il riepilogo. Un solo destinatario, fisso, mai dedotto dai messaggi."""

    to: str = ""
    sender: str = ""
    subject_prefix: str = "PEC"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_password_source: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout: int = 30
    #: scrive comunque una copia su file, utile per diagnosi e per gli archivi
    copy_dir: Path | None = None
    #: se falso non si spedisce niente: si scrive solo la copia su file
    send: bool = True


@dataclass(frozen=True)
class Config:
    #: radice di output di pecfetch: si legge `coda/`, si scrive solo `esiti/`
    output_root: Path
    #: archivio storico di pecfetch, aperto SEMPRE in sola lettura
    archive_path: Path
    #: stato locale di pecdesk, separato da quello di pecfetch
    state_dir: Path
    #: dove finisce ciò che è stato lavorato, fuori dalla coda
    worked_dir: Path

    rules_path: Path
    instructions_path: Path

    api_key: str = field(default="", repr=False)
    api_key_source: str = ""
    model: str = DEFAULT_MODEL
    max_tokens: int = 400

    material: MaterialLimits = field(default_factory=MaterialLimits)
    suspicion: SuspicionLimits = field(default_factory=SuspicionLimits)
    api: ApiSettings = field(default_factory=ApiSettings)
    digest: DigestSettings = field(default_factory=DigestSettings)

    log_file: Path | None = None
    log_level: str = "INFO"
    #: lo stesso fuso di pecfetch: le due metà scrivono nello stesso albero
    timezone: str = tempo.DEFAULT_TZ
    #: lo stesso modello di permessi di pecfetch, per gli esiti e i lavorati
    permissions: perm.Permessi = perm.Permessi()
    #: dopo N tentativi falliti un messaggio resta in coda ma non si ritenta
    max_attempts: int = 3

    source_path: Path | None = None

    @property
    def tz(self):
        """Il fuso già risolto. Validato al caricamento, qui non può fallire."""
        return tempo.zona(self.timezone)

    @property
    def outcomes_dir(self) -> Path:
        return self.output_root / "esiti"

    @property
    def queue_dir(self) -> Path:
        return self.output_root / "coda"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "stato.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "pecdesk.lock"


DEFAULT_PATHS = (
    Path("/etc/pecdesk/pecdesk.toml"),
    Path("config/pecdesk.toml"),
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
        # come in pecfetch.config: un problema di permessi è un problema di
        # configurazione, non una traccia di stack
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


def _resolve_secret(raw: dict, base: str, warn, what: str,
                    required: bool = True) -> tuple[str, str]:
    """Legge un segreto da ambiente o da file. Il valore non viene mai loggato."""
    env_key = f"{base}_env"
    file_key = f"{base}_file"
    if env_key in raw:
        env = str(raw[env_key])
        value = os.environ.get(env, "")
        if not value and required:
            raise ConfigError(f"{what}: variabile d'ambiente {env} non valorizzata")
        return value, f"env:{env}"
    if file_key in raw:
        path = Path(str(raw[file_key])).expanduser()
        _warn_if_world_readable(path, warn)
        try:
            return path.read_text(encoding="utf-8").strip(), f"file:{path}"
        except OSError as exc:
            raise ConfigError(f"{what}: {exc}") from exc
    if base in raw:
        warn(f"{what}: segreto in chiaro nel file di configurazione; "
             f"usa {env_key} oppure {file_key}")
        return str(raw[base]), "inline"
    if required:
        raise ConfigError(f"{what}: nessun segreto configurato "
                          f"(usa {env_key} oppure {file_key})")
    return "", "assente"


def load_config(path: str | os.PathLike | None = None, warn=None) -> Config:
    """Carica la configurazione. `warn` riceve gli avvisi non fatali."""
    warn = warn or (lambda msg: None)

    candidates = [Path(path)] if path else list(DEFAULT_PATHS)
    config_path = next((p for p in candidates if p.is_file()), None)
    if config_path is None:
        raise ConfigError(
            "nessun file di configurazione trovato (cercati: "
            + ", ".join(str(p) for p in candidates) + ")"
        )
    data = _load_toml(config_path)
    base = config_path.parent

    def as_path(value) -> Path:
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    general = data.get("general", {})
    if "output_root" not in general:
        raise ConfigError("manca [general].output_root")
    output_root = as_path(general["output_root"])
    state_dir = as_path(general.get("state_dir", "/var/lib/pecdesk"))
    archive_path = (
        as_path(general["archive_path"]) if general.get("archive_path")
        else Path("/var/lib/pecfetch/archivio.sqlite3")
    )
    worked_dir = (
        as_path(general["worked_dir"]) if general.get("worked_dir")
        else output_root / "lavorati"
    )

    direttive = data.get("direttive", {})
    rules_path = as_path(direttive.get("regole", "direttive/regole.toml"))
    instructions_path = as_path(direttive.get("istruzioni", "direttive/istruzioni.md"))

    model_cfg = data.get("modello", {})
    api_key, api_key_source = _resolve_secret(
        model_cfg, "api_key", warn, "chiave API Anthropic", required=False
    )

    mat = data.get("materiale", {})
    material = MaterialLimits(
        body_chars=int(mat.get("corpo_caratteri", 1_500)),
        attachment_chars=int(mat.get("allegato_caratteri", 1_200)),
        attachment_head_chars=int(mat.get("allegato_testa_caratteri", 600)),
        keyword_windows=int(mat.get("finestre", 4)),
        keyword_window_chars=int(mat.get("finestra_caratteri", 240)),
        max_attachments=int(mat.get("allegati_massimi", 3)),
        total_chars=int(mat.get("totale_caratteri", 4_000)),
    )

    sus = data.get("sospetto", {})
    suspicion = SuspicionLimits(
        min_history_days=int(sus.get("storico_minimo_giorni", 90)),
        min_history_messages=int(sus.get("storico_minimo_messaggi", 20)),
    )

    api_cfg = data.get("api", {})
    api = ApiSettings(
        max_retries=int(api_cfg.get("ritentativi", 3)),
        timeout_seconds=float(api_cfg.get("timeout_secondi", 60.0)),
        max_messages=int(api_cfg.get("messaggi_massimi", 200)),
        stop_after_failures=int(api_cfg.get("fallimenti_consecutivi_massimi", 5)),
        pause_seconds=float(api_cfg.get("pausa_secondi", 0.0)),
    )

    dig = data.get("riepilogo", {})
    send = bool(dig.get("invia", True))
    smtp_password, smtp_source = ("", "assente")
    if send and dig.get("smtp_host"):
        smtp_password, smtp_source = _resolve_secret(
            dig, "smtp_password", warn, "riepilogo/SMTP", required=False
        )
    digest = DigestSettings(
        to=str(dig.get("destinatario", "")).strip(),
        sender=str(dig.get("mittente", "")).strip(),
        subject_prefix=str(dig.get("prefisso_oggetto", "PEC")),
        smtp_host=str(dig.get("smtp_host", "")).strip(),
        smtp_port=int(dig.get("smtp_port", 587)),
        smtp_user=str(dig.get("smtp_user", "")).strip(),
        smtp_password=smtp_password,
        smtp_password_source=smtp_source,
        smtp_starttls=bool(dig.get("smtp_starttls", True)),
        smtp_ssl=bool(dig.get("smtp_ssl", False)),
        smtp_timeout=int(dig.get("smtp_timeout", 30)),
        copy_dir=as_path(dig["copia_in"]) if dig.get("copia_in") else None,
        send=send,
    )
    if send and not digest.to:
        warn("[riepilogo].destinatario non impostato: il riepilogo non partirà")

    logs = data.get("logging", {})
    log_file = as_path(logs["file"]) if logs.get("file") else None

    # Stesso fuso e stessi permessi di pecfetch: le due metà scrivono nello
    # stesso albero, e due convenzioni diverse lo renderebbero illeggibile.
    timezone_name = str(general.get("timezone", tempo.DEFAULT_TZ))
    try:
        tempo.zona(timezone_name)
    except ValueError as exc:
        raise ConfigError(f"[general].timezone: {exc}") from exc

    perms_raw = data.get("permissions", {})
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

    return Config(
        output_root=output_root,
        archive_path=archive_path,
        state_dir=state_dir,
        worked_dir=worked_dir,
        rules_path=rules_path,
        instructions_path=instructions_path,
        api_key=api_key,
        api_key_source=api_key_source,
        model=str(model_cfg.get("id", DEFAULT_MODEL)),
        max_tokens=int(model_cfg.get("max_tokens", 400)),
        material=material,
        suspicion=suspicion,
        api=api,
        digest=digest,
        log_file=log_file,
        log_level=str(logs.get("level", "INFO")).upper(),
        timezone=timezone_name,
        permissions=permissions,
        max_attempts=int(general.get("tentativi_massimi", 3)),
        source_path=config_path,
    )


def redacted(cfg: Config) -> dict:
    """Vista della configurazione sicura da loggare: nessun segreto, mai."""
    return {
        "config": str(cfg.source_path),
        "output_root": str(cfg.output_root),
        "coda": str(cfg.queue_dir),
        "esiti": str(cfg.outcomes_dir),
        "lavorati": str(cfg.worked_dir),
        "archivio": f"{cfg.archive_path} (sola lettura)",
        "state_dir": str(cfg.state_dir),
        "direttive": {"regole": str(cfg.rules_path),
                      "istruzioni": str(cfg.instructions_path)},
        "modello": {"id": cfg.model, "max_tokens": cfg.max_tokens,
                    "api_key": f"<{cfg.api_key_source}>",
                    "ritentativi": cfg.api.max_retries},
        "fuso": cfg.timezone,
        "permessi": perm.descrizione(cfg.permissions),
        "riepilogo": {"destinatario": cfg.digest.to or "<non impostato>",
                      "smtp": f"{cfg.digest.smtp_host}:{cfg.digest.smtp_port}"
                              if cfg.digest.smtp_host else "<non impostato>",
                      "smtp_password": f"<{cfg.digest.smtp_password_source}>",
                      "invia": cfg.digest.send},
    }


__all__ = ["Config", "ConfigError", "MaterialLimits", "SuspicionLimits",
           "ApiSettings", "DigestSettings", "load_config", "redacted",
           "DEFAULT_MODEL"]
