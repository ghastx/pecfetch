"""Gli esiti: spazio del consumatore, append-only, immutabile all'indietro.

``esiti/`` è la cartella che il contratto di pecfetch riserva a questo programma.
Ci si scrive una riga per messaggio, con la stessa tecnica dell'indice di
pecfetch — ``O_APPEND`` più ``fsync``, nessun lock — così che una riga o c'è
tutta o non c'è.

Le correzioni dell'utente vanno **accanto** alla proposta, mai al suo posto:
stanno in ``esiti/correzioni/`` e portano sia il valore proposto sia quello
corretto. Un registro che sovrascrive gli errori non serve a capire dove il
sistema sbaglia in modo sistematico, che è tutto il motivo per cui esiste.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pecfetch import permessi as perm, tempo

SCHEMA_OUTCOME = "pecdesk/esito/1"
SCHEMA_CORRECTION = "pecdesk/correzione/1"

DIR_CORRECTIONS = "correzioni"

#: classi dell'esito, dalla più urgente alla più ordinaria
CLASSES = ("sospetto", "attenzione", "inoltro", "ordinario", "non_classificato")


@dataclass
class Outcome:
    """Un esito. È quello che finisce nel file e quello che legge il riepilogo."""

    id: str
    processed_at: str
    account: str = ""
    client: str = ""
    mailbox: str = ""
    sender: str = ""
    subject: str = ""
    date: str = ""

    doc_type: str = "altro"
    klass: str = "ordinario"
    route: str = "nessuno"
    recipient_alias: str = ""
    recipient: str = ""
    route_origin: str = ""
    owner_attention: bool = False

    deadline: dict | None = None
    suspicion_level: str = "nessuno"
    suspicion_signals: list[str] = field(default_factory=list)
    suspicion_details: dict = field(default_factory=dict)

    reason: str = ""
    confidence: str = "media"
    uncertainty: list[str] = field(default_factory=list)
    uncertain: bool = False

    rules_applied: list[str] = field(default_factory=list)
    label: str = ""
    directives: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    material: dict = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> dict:
        record: dict = {
            "schema": SCHEMA_OUTCOME,
            "id": self.id,
            "elaborato_il": self.processed_at,
            "casella": self.account,
            "cliente": self.client,
            "casella_etichetta": self.mailbox,
            "mittente": self.sender,
            "oggetto": self.subject,
            "data": self.date,
            "tipo_documento": self.doc_type,
            "classe": self.klass,
            "instradamento": {
                "tipo": self.route,
                "alias": self.recipient_alias,
                "a": self.recipient,
                "origine": self.route_origin,
            },
            "intervento_titolare": self.owner_attention,
            "sospetto": {
                "livello": self.suspicion_level,
                "segnali": list(self.suspicion_signals),
            },
            "motivazione": self.reason,
            "confidenza": {"livello": self.confidence,
                           "motivi": list(self.uncertainty)},
            "incerto": self.uncertain,
            "direttive": dict(self.directives),
        }
        if self.deadline is not None:
            record["termine"] = dict(self.deadline)
        if self.suspicion_details:
            record["sospetto"]["dettagli"] = dict(self.suspicion_details)
        if self.rules_applied:
            record["regole_applicate"] = list(self.rules_applied)
        if self.label:
            record["etichetta"] = self.label
        if self.model:
            record["modello"] = dict(self.model)
        if self.material:
            record["materiale"] = dict(self.material)
        if self.error:
            record["errore"] = self.error
        return record

    @classmethod
    def from_json(cls, record: dict) -> "Outcome":
        routing = record.get("instradamento", {}) or {}
        suspicion = record.get("sospetto", {}) or {}
        confidence = record.get("confidenza", {}) or {}
        return cls(
            id=str(record.get("id", "")),
            processed_at=str(record.get("elaborato_il", "")),
            account=str(record.get("casella", "")),
            client=str(record.get("cliente", "")),
            mailbox=str(record.get("casella_etichetta", "")),
            sender=str(record.get("mittente", "")),
            subject=str(record.get("oggetto", "")),
            date=str(record.get("data", "")),
            doc_type=str(record.get("tipo_documento", "altro")),
            klass=str(record.get("classe", "ordinario")),
            route=str(routing.get("tipo", "nessuno")),
            recipient_alias=str(routing.get("alias", "")),
            recipient=str(routing.get("a", "")),
            route_origin=str(routing.get("origine", "")),
            owner_attention=bool(record.get("intervento_titolare", False)),
            deadline=record.get("termine"),
            suspicion_level=str(suspicion.get("livello", "nessuno")),
            suspicion_signals=list(suspicion.get("segnali", []) or []),
            suspicion_details=dict(suspicion.get("dettagli", {}) or {}),
            reason=str(record.get("motivazione", "")),
            confidence=str(confidence.get("livello", "media")),
            uncertainty=list(confidence.get("motivi", []) or []),
            uncertain=bool(record.get("incerto", False)),
            rules_applied=list(record.get("regole_applicate", []) or []),
            label=str(record.get("etichetta", "")),
            directives=dict(record.get("direttive", {}) or {}),
            model=dict(record.get("modello", {}) or {}),
            material=dict(record.get("materiale", {}) or {}),
            error=str(record.get("errore", "")),
        )


class OutcomeStore:
    """Unico punto di scrittura in ``esiti/``. I file di input non si toccano."""

    def __init__(self, root: Path, timezone: str = tempo.DEFAULT_TZ,
                 permissions: perm.Permessi | None = None):
        self.root = Path(root)
        # un fuso sbagliato è un errore di configurazione, non un ripiego su UTC
        self.tz = tempo.zona(timezone)
        self.permessi = permissions or perm.Permessi()
        # esiti/ è condivisa: pecfetch la crea, pecdesk ci scrive
        perm.crea_dir(self.root, self.permessi.shared_dir_mode, self.permessi)
        perm.crea_dir(self.root / DIR_CORRECTIONS, self.permessi.shared_dir_mode,
                      self.permessi)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def path_for(self, when: datetime | None = None) -> Path:
        when = when or self.now()
        return self.root / f"{when:%Y-%m-%d}.jsonl"

    def corrections_path(self, when: datetime | None = None) -> Path:
        when = when or self.now()
        return self.root / DIR_CORRECTIONS / f"{when:%Y-%m-%d}.jsonl"

    def _append(self, path: Path, record: dict) -> None:
        """Append per riga, con `fsync`: una riga o c'è tutta o non c'è.

        Se l'ultima riga è rimasta a metà — un'interruzione fra la scrittura e
        il `fsync` — si va a capo prima di scrivere. Altrimenti la riga monca si
        mangerebbe anche quella nuova, e si perderebbe un esito già prodotto per
        colpa di uno già perso.
        """
        perm.crea_dir(path.parent, self.permessi.shared_dir_mode, self.permessi)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = perm.apri_append(path, self.permessi, rileggibile=True)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = "\n" + line
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def append(self, outcome: Outcome, when: datetime | None = None) -> Path:
        path = self.path_for(when)
        self._append(path, outcome.to_json())
        return path

    def append_correction(self, msg_id: str, field_name: str, proposed,
                          corrected, author: str = "", note: str = "",
                          when: datetime | None = None) -> Path:
        record = {
            "schema": SCHEMA_CORRECTION,
            "id": msg_id,
            "corretto_il": (when or self.now()).isoformat(timespec="seconds"),
            "campo": field_name,
            "proposto": proposed,
            "corretto": corrected,
            "autore": author,
            "nota": note,
        }
        path = self.corrections_path(when)
        self._append(path, record)
        return path

    # -- lettura ---------------------------------------------------------
    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        records: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # riga tagliata da un'interruzione
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []
        return records

    def _day_files(self, directory: Path, days: int | None,
                   since: str = "", until: str = "") -> list[Path]:
        if not directory.is_dir():
            return []
        files = sorted(p for p in directory.glob("*.jsonl") if p.is_file())
        if since:
            files = [p for p in files if p.stem >= since]
        if until:
            files = [p for p in files if p.stem <= until]
        if days is not None:
            files = files[-days:]
        return files

    def read(self, days: int | None = None, since: str = "",
             until: str = "") -> list[Outcome]:
        out: list[Outcome] = []
        for path in self._day_files(self.root, days, since, until):
            out += [Outcome.from_json(r) for r in self._read_jsonl(path)]
        return out

    def read_corrections(self, days: int | None = None, since: str = "",
                         until: str = "") -> list[dict]:
        records: list[dict] = []
        for path in self._day_files(self.root / DIR_CORRECTIONS, days, since, until):
            records += self._read_jsonl(path)
        return records

    def known_ids(self, days: int = 7) -> set[str]:
        """Gli id già scritti negli ultimi giorni. Serve alla riconciliazione:
        un esito scritto ma non registrato nello stato non si riclassifica."""
        return {o.id for o in self.read(days=days) if o.id}

    def find(self, msg_id: str, days: int | None = None) -> list[Outcome]:
        return [o for o in self.read(days=days) if o.id == msg_id]


__all__ = ["Outcome", "OutcomeStore", "SCHEMA_OUTCOME", "SCHEMA_CORRECTION",
           "CLASSES", "DIR_CORRECTIONS"]
