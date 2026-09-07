"""Costruzione di una coda pecfetch finta, e di un modello finto.

La coda è finta ma ha la forma vera: le stesse chiavi che scrive
``pecfetch.output.OutputWriter``, perché il contratto fra i due programmi è
esattamente ciò che questi test devono difendere.

Nessun test tocca la rete, né l'API né l'SMTP.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pecdesk.model import Judgment  # noqa: E402

REGOLE = """
versione = "test-1"

tipi_documento = ["fattura", "sollecito_pagamento", "atto_giudiziario",
                  "cartella_esattoriale", "avviso_accertamento", "notifica_ente",
                  "contributi_inps", "ricevuta_negativa",
                  "comunicazione_ordinaria", "altro"]
tipi_sempre_al_titolare = ["atto_giudiziario", "cartella_esattoriale",
                           "avviso_accertamento"]

[destinatari]
titolare = "titolare@studio.it"
contabilita = "contabilita@studio.it"
paghe = "paghe@studio.it"
segreteria = "segreteria@studio.it"

[parole_chiave]
termini = ["entro il", "termine", "scadenza", "ricorso", "iban", "sollecito"]

[[regola]]
id = "riscossione"
priorita = 100
quando = { mittente_dominio = ["agenziariscossione.gov.it"] }
allora = { instradamento = "titolare", attenzione = true, etichetta = "riscossione" }

[[regola]]
id = "inps"
priorita = 90
quando = { mittente_dominio = ["postacert.inps.gov.it"] }
allora = { instradamento = "studio", a = "paghe", etichetta = "contributi" }

[[regola]]
id = "ricevute-negative"
priorita = 80
quando = { tipo_pecfetch = ["errore_consegna"] }
allora = { instradamento = "studio", a = "segreteria", salta_modello = true }

[[regola]]
id = "fornitore-noto"
priorita = 10
quando = { mittente = ["fornitore@pec.it"] }
allora = { instradamento = "studio", a = "contabilita" }
"""

ISTRUZIONI = """# Istruzioni dello studio

Le fatture vanno in contabilità. Gli atti li guardo io.
Se un termine non si legge chiaramente, non inventarlo: dimmi che c'è e basta.
"""


# ---------------------------------------------------------------------------
# costruzione di una coda finta
# ---------------------------------------------------------------------------

def make_attachment(name: str, *, text: str = "", method: str = "pdf_text",
                    status: str = "ok", content_type: str = "application/pdf",
                    size: int = 12_345, stored: bool = True,
                    active: bool = False, suspicious: bool = False,
                    note: str = "", archive: dict | None = None) -> dict:
    entry: dict = {
        "nome": name,
        "nome_file": name,
        "percorso": f"allegati/{name}",
        "content_type": content_type,
        "byte": size,
        "sha256": "0" * 64,
        "salvato": stored,
        "testo": {"method": method, "status": status, "chars": len(text)},
    }
    if text:
        entry["testo"]["percorso"] = f"allegati/{name}.txt"
    if active:
        entry["contenuto_attivo"] = True
    if suspicious:
        entry["contenuto_sospetto"] = True
    if note:
        entry["nota"] = note
    if archive is not None:
        entry["archivio"] = archive
    entry["_testo_grezzo"] = text          # solo per la fixture, rimosso a scrittura
    return entry


def write_message(queue_dir: Path, msg_id: str, *, sender: str = "cliente@pec.it",
                  sender_name: str = "Cliente S.r.l.", subject: str = "Comunicazione",
                  body: str = "Buongiorno, in allegato quanto richiesto.",
                  account: str = "rossi", client: str = "ROSSI",
                  msg_type: str = "posta_certificata", certified: bool = True,
                  date: str = "2026-09-07T08:30:00+02:00",
                  attachments: list[dict] | None = None,
                  notes: list[str] | None = None,
                  receipt: dict | None = None,
                  body_truncated: bool = False) -> Path:
    """Scrive una cartella di messaggio con la forma prodotta da pecfetch."""
    attachments = attachments or []
    folder = queue_dir / f"{date[:10].replace('-', '')}_{account}_{msg_id[:12]}"
    (folder / "allegati").mkdir(parents=True, exist_ok=True)
    (folder / "corpo.txt").write_text(body, encoding="utf-8")
    (folder / "busta.eml").write_bytes(b"From: x\r\n\r\nbusta finta")

    clean: list[dict] = []
    for raw in attachments:
        entry = dict(raw)
        text = entry.pop("_testo_grezzo", "")
        if entry.get("salvato", True):
            (folder / "allegati" / entry["nome"]).write_bytes(b"%PDF-finto")
        if text:
            (folder / "allegati" / f"{entry['nome']}.txt").write_text(
                text, encoding="utf-8")
        clean.append(entry)

    record = {
        "schema": "pecfetch/messaggio/1",
        "generato_da": "pecfetch/1.0.0",
        "id": msg_id,
        "acquisito_il": date,
        "casella": {"id": account, "etichetta": f"{client} S.r.l.",
                    "indirizzo": f"{account}@pec.it", "cliente": client},
        "tipo": msg_type,
        "certificato": certified,
        "data": {"certificata": date, "invio": date, "ricezione": date},
        "mittente": {"indirizzo": sender, "nome": sender_name,
                     "dominio": sender.split("@", 1)[-1]},
        "destinatari": [f"{account}@pec.it"],
        "copia": [],
        "oggetto": subject,
        "allegati": clean,
        "message_id": f"<{msg_id}@pec.it>",
        "identificativo_pec": f"opec.{msg_id}",
        "gestore": "Gestore S.p.A.",
        "contenuto": {"cartella": f"coda/{folder.name}",
                      "corpo": f"coda/{folder.name}/corpo.txt",
                      "caratteri_corpo": len(body),
                      "corpo_troncato": body_truncated,
                      "corpo_origine": "text/plain"},
        "imap": {"uidvalidity": 1, "uid": 1},
    }
    if notes:
        record["note"] = notes
    if receipt:
        record["ricevuta"] = receipt
    (folder / "messaggio.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return folder


# ---------------------------------------------------------------------------
# il modello, finto
# ---------------------------------------------------------------------------

@dataclass
class FakeClassifier:
    """Il modello, finto. Nessuna rete, e si può far fallire a comando."""

    judgment: Judgment = field(default_factory=Judgment)
    raises: Exception | None = None
    calls: list = field(default_factory=list)
    per_id: dict = field(default_factory=dict)

    def classify(self, request):
        self.calls.append(request)
        if self.raises is not None:
            raise self.raises
        for key, value in self.per_id.items():
            if key in request.user:
                return value
        return self.judgment

    def count_tokens(self, request) -> int:
        return request.estimated_tokens()
