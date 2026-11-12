"""Independent private diagnostics; legacy journal translation is unchanged."""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid5

from ctrunner.eventlog import Event
from ctrunner.protocol import EventKind, JsonValue
from ctrunner.turn_lifecycle import TurnLiveness
from ctrunner.v0 import parse_row, usage_draft

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
        return identity in self.known_ids((identity,))

    def known_ids(self, identities: Sequence[str]) -> set[str]:
        if not identities:
            return set()
        # A hot window/page is bounded to 256; reject accidental unbounded callers.
        if len(identities) > 512:
            raise ValueError("diagnostic_receipt_batch_too_large")
        placeholders = ",".join("?" for _ in identities)
        with closing(self._connect()) as db:
            return {row[0] for row in db.execute(
                f"SELECT id FROM receipts WHERE id IN ({placeholders})", tuple(identities)  # noqa: S608 — placeholders only; IDs are bound
            )}

    def record(self, identity: str, status: str) -> None:
        self.record_many({identity: status})

    def record_many(self, outcomes: Mapping[str, str]) -> None:
        if not outcomes:
            return
        if len(outcomes) > 256:
            raise ValueError("diagnostic_receipt_batch_too_large")
        if any(status not in {"accepted", "rejected"} for status in outcomes.values()):
            raise ValueError("invalid_diagnostic_receipt")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in outcomes)
            existing = dict(db.execute(
                f"SELECT id,status FROM receipts WHERE id IN ({placeholders})", tuple(outcomes)  # noqa: S608 — placeholders only; IDs are bound
            ))
            if any(existing[key] != outcomes[key] for key in existing):
                raise ValueError("conflicting_diagnostic_receipt")
            db.executemany("INSERT INTO receipts VALUES(?,?) ON CONFLICT(id) DO NOTHING",
                           outcomes.items())


class DiagnosticFeed:
    """Replay retained rows independently of public ACK; never modifies v0 drafts."""

    def __init__(self, path: Path, project: str, root: str):
        self._path, self._project = path, project
        # Only IDs/liveness are needed; never retain roles, prompts, paths or tool content.
        self._liveness = TurnLiveness()
        self._offset = 0
        self.scan_pending = False

    def _usage_is_live(self, event: Event) -> bool:
        self._liveness.feed(event)
        payload = event.payload if isinstance(event.payload, dict) else {}
        return (event.kind is EventKind.SDK and self._liveness.live(event.turn_id)
                and payload.get("type") == "result")

    def rewind(self) -> None:
        """Begin another retained-journal pass without treating silence as a receipt."""
        self._offset = 0
        self.scan_pending = False
        self._liveness = TurnLiveness()

    def caught_up(self) -> bool:
        """All retained bytes consumed; an incomplete final row waits for the deadline."""
        try:
            return self._offset >= self._path.stat().st_size
        except FileNotFoundError:
            return True

    def poll(self, max_frames: int = 256) -> list[dict[str, JsonValue]]:
        # Distinguish a bounded scan page from EOF/incomplete input. The caller can
        # cooperatively continue scanning without waiting for another journal append.
        self.scan_pending = False
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
                self.scan_pending = (
                    (len(lines) == max_frames or size >= 1_048_576)
                    and stream.tell() < self._path.stat().st_size
                )
        except FileNotFoundError:
            return []
        frames: list[dict[str, JsonValue]] = []
        for line in lines:
            self._offset += len(line)
            if not line.strip():
                continue
            try:
                # Public payload still uses the legacy parser; money remains exact here.
                event = parse_row(json.loads(line))
                if not self._usage_is_live(event):
                    continue
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
            draft = usage_draft(self._project, event)
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
