"""Independent private diagnostics; legacy journal translation is unchanged."""

import json
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid5

from ctrunner.protocol import JsonValue
from ctrunner.v0 import EventType, Translator, parse_row

SCHEMA = "harness.diagnostic.v1"
DIAGNOSTIC_NAMESPACE = UUID("9439952e-1b67-49a9-9703-93791c6e46cd")
SESSION_NAMESPACE = UUID("ed34fba5-d12a-4c7b-b48f-773dad781c07")


def identity(project: str, run: str, source: str) -> str:
    return str(
        uuid5(
            DIAGNOSTIC_NAMESPACE,
            json.dumps([SCHEMA, project, run, source], ensure_ascii=False, separators=(",", ":")),
        )
    )


def amount(raw: object) -> str | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, Decimal)):
        return None
    try:
        value = Decimal(raw)
        if (
            not value.is_finite()
            or value < 0
            or value.adjusted() > 63
            or value.as_tuple().exponent < -63
        ):
            return None
        text = (
            format(value, "f").rstrip("0").rstrip(".")
            if "." in format(value, "f")
            else format(value, "f")
        )
        text = "0" if value == 0 else text
        return text if len(text) <= 64 else None
    except (InvalidOperation, ValueError):
        return None


class DiagnosticReceipts:
    """Only definitive private outcomes; no source payload or public cursor state."""

    def __init__(self, journal: Path):
        self._path = journal.with_name(journal.name + ".diagnostic-receipts.sqlite3")
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts ("
                       "id TEXT PRIMARY KEY, status TEXT NOT NULL "
                       "CHECK(status IN ('accepted','rejected')))")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=1)

    def contains(self, identity: str) -> bool:
        with closing(self._connect()) as db:
            row = db.execute("SELECT 1 FROM receipts WHERE id=?", (identity,)).fetchone()
            return row is not None

    def record(self, identity: str, status: str) -> None:
        if status not in {"accepted", "rejected"}:
            raise ValueError("invalid_diagnostic_receipt")
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO receipts VALUES(?,?) ON CONFLICT(id) DO NOTHING",
                       (identity, status))


class DiagnosticFeed:
    """Replay retained rows independently of public ACK; never modifies v0 drafts."""

    def __init__(self, path: Path, project: str, root: str):
        self._path, self._project = path, project
        self._translator = Translator(project, root)
        self._offset = 0

    def poll(self, max_frames: int = 256) -> list[dict[str, JsonValue]]:
        if max_frames <= 0:
            return []
        try:
            with self._path.open("rb") as stream:
                stream.seek(self._offset)
                # Bounded scan pages; retain an incomplete final row for the next poll.
                lines: list[bytes] = []
                size = 0
                while len(lines) < max_frames and size < 1_048_576:
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        break
                    lines.append(line)
                    size += len(line)
        except FileNotFoundError:
            return []
        frames: list[dict[str, JsonValue]] = []
        for line in lines:
            self._offset += len(line)
            if not line.strip():
                continue
            try:
                # Separate parses deliberately preserve legacy float translation.
                event = parse_row(json.loads(line))
                drafts = self._translator.feed(event)
                exact = json.loads(line, parse_float=Decimal)
            except ValueError:
                continue
            raw = exact.get("payload")
            if not isinstance(raw, dict) or raw.get("type") != "result":
                continue
            session = raw.get("session_id")
            value = amount(raw.get("total_cost_usd"))
            if not isinstance(session, str) or not session or len(session) > 512 or value is None:
                continue
            for draft in drafts:
                if draft.type != EventType.USAGE_REPORTED:
                    continue
                ref = str(
                    uuid5(
                        SESSION_NAMESPACE,
                        json.dumps(
                            [self._project, session], ensure_ascii=False, separators=(",", ":")
                        ),
                    )
                )
                frames.append(
                    {
                        "v": 1,
                        "type": "harness.diagnostic",
                        "schema": SCHEMA,
                        "diagnostic_id": identity(self._project, draft.run_id, draft.event_id),
                        "project_id": self._project,
                        "transport_run_id": draft.run_id,
                        "source_event_id": draft.event_id,
                        "source_journal_seq": event.seq,
                        "payload": {
                            "session_ref": ref,
                            "observed_at": draft.occurred_at.isoformat().replace("+00:00", "Z"),
                            "value_usd": value,
                        },
                    }
                )
        return frames
