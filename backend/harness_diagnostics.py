"""Private transport receipts. No public sequence, cursor or accounting effects."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5

SCHEMA = "harness.diagnostic.v1"
NAMESPACE = UUID("9439952e-1b67-49a9-9703-93791c6e46cd")


class DiagnosticError(ValueError):
    pass


def diagnostic_identity(project: str, run: str, source: str) -> str:
    return str(uuid5(NAMESPACE, json.dumps([SCHEMA, project, run, source],
        ensure_ascii=False, separators=(",", ":"))))


def initialize(db: sqlite3.Connection) -> None:
    # This is the existing transport's additive SQLite bootstrap, not PostgreSQL DDL.
    db.executescript("""
      CREATE TABLE IF NOT EXISTS diagnostic_segments (
        command_id TEXT PRIMARY KEY, project TEXT NOT NULL, transport_run TEXT NOT NULL,
        backend_run TEXT NOT NULL UNIQUE, start_after_public_seq INTEGER NOT NULL,
        end_public_seq INTEGER, state TEXT NOT NULL CHECK(state IN ('pending','active','closed')));
      CREATE UNIQUE INDEX IF NOT EXISTS diagnostic_one_open
        ON diagnostic_segments(project,transport_run) WHERE state IN ('pending','active');
      CREATE TABLE IF NOT EXISTS diagnostic_sources (
        source_event_id TEXT PRIMARY KEY, project TEXT NOT NULL, transport_run TEXT NOT NULL,
        command_id TEXT, source_journal_seq INTEGER NOT NULL, source_public_seq INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS diagnostic_receipts (
        diagnostic_id TEXT PRIMARY KEY, schema TEXT NOT NULL, canonical_body TEXT NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE, accepted_at TEXT NOT NULL);
    """)


def segment(db: sqlite3.Connection, project: str, run: str, command: str,
            backend: str | None, *, pending: bool) -> None:
    if backend is None:
        return
    if not isinstance(backend, str) or not backend or len(backend) > 64:
        raise DiagnosticError("invalid_backend_binding")
    boundary = db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run=?", (run,)).fetchone()[0]
    db.execute("INSERT INTO diagnostic_segments VALUES(?,?,?,?,?,NULL,?)",
               (command, project, run, backend, boundary, "pending" if pending else "active"))


def observe(db: sqlite3.Connection, project: str, journal_seq: int, event: dict) -> None:
    run, kind = event["run_id"], event["type"]
    current = db.execute("SELECT * FROM diagnostic_segments WHERE project=? AND transport_run=? "
                         "AND state IN ('pending','active')", (project, run)).fetchone()
    public_seq = db.execute("SELECT seq FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
    if public_seq is None:
        return
    seq = public_seq[0]
    if kind == "usage.reported":
        command = current["command_id"] if current and current["state"] == "active" else None
        db.execute("INSERT INTO diagnostic_sources VALUES(?,?,?,?,?,?)",
                   (event["event_id"], project, run, command, journal_seq, seq))
    if current and kind == "user.answer.accepted" and current["state"] == "pending":
        db.execute("UPDATE diagnostic_segments SET state='active',start_after_public_seq=? WHERE command_id=?",
                   (seq, current["command_id"]))
    if current and (kind == "facilitator.question" or kind in {
            "run.completed", "run.failed", "run.outcome_unknown"}):
        db.execute("UPDATE diagnostic_segments SET state='closed',end_public_seq=? WHERE command_id=?",
                   (seq, current["command_id"]))


def parse(frame: object, project: str) -> tuple[dict, str]:
    fields = {"v", "type", "schema", "diagnostic_id", "project_id", "transport_run_id",
              "source_event_id", "source_journal_seq", "payload"}
    if not isinstance(frame, dict) or set(frame) != fields:
        raise DiagnosticError("invalid_diagnostic")
    if type(frame["v"]) is not int or frame["v"] != 1 or frame["type"] != "harness.diagnostic" or frame["schema"] != SCHEMA:
        raise DiagnosticError("invalid_diagnostic")
    if frame["project_id"] != project or not isinstance(frame["transport_run_id"], str):
        raise DiagnosticError("diagnostic_project_mismatch")
    payload = frame["payload"]
    if not isinstance(payload, dict) or set(payload) != {"session_ref", "observed_at", "value_usd"}:
        raise DiagnosticError("invalid_diagnostic")
    if type(frame["source_journal_seq"]) is not int or frame["source_journal_seq"] < 1:
        raise DiagnosticError("invalid_diagnostic")
    try:
        UUID(frame["source_event_id"])
        UUID(payload["session_ref"])
        stamp = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
        raw = payload["value_usd"]
        if not isinstance(raw, str) or not 1 <= len(raw) <= 64:
            raise ValueError
        value = Decimal(raw)
        if (not value.is_finite() or value < 0 or stamp.tzinfo is None
                or value.adjusted() > 63 or value.as_tuple().exponent < -63):
            raise ValueError
        canonical = format(value, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        canonical = "0" if value == 0 else canonical
        if raw != canonical:
            raise ValueError
        if frame["diagnostic_id"] != diagnostic_identity(project, frame["transport_run_id"], frame["source_event_id"]):
            raise ValueError
    except (TypeError, AttributeError, ValueError, InvalidOperation):
        raise DiagnosticError("invalid_diagnostic") from None
    return frame, json.dumps(frame, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def accept(db: sqlite3.Connection, frame: dict, canonical: str, now: str) -> dict | None:
    seen = db.execute("SELECT canonical_body FROM diagnostic_receipts WHERE diagnostic_id=?",
                      (frame["diagnostic_id"],)).fetchone()
    if seen:
        if seen[0] != canonical:
            raise DiagnosticError("diagnostic_conflict")
    else:
        source = db.execute("SELECT s.* FROM diagnostic_sources s JOIN diagnostic_segments g "
            "ON g.command_id=s.command_id WHERE s.source_event_id=?", (frame["source_event_id"],)).fetchone()
        if source is None:
            return None
        if (source["project"], source["transport_run"], source["source_journal_seq"]) != (
                frame["project_id"], frame["transport_run_id"], frame["source_journal_seq"]):
            raise DiagnosticError("diagnostic_source_mismatch")
        event = db.execute("SELECT body FROM events WHERE event_id=?", (frame["source_event_id"],)).fetchone()
        if event is None or json.loads(event[0])["occurred_at"] != frame["payload"]["observed_at"]:
            raise DiagnosticError("diagnostic_source_mismatch")
        db.execute("INSERT INTO diagnostic_receipts VALUES(?,?,?,?,?)",
                   (frame["diagnostic_id"], SCHEMA, canonical, frame["source_event_id"], now))
    return {"v": 1, "type": "harness.diagnostic_ack", "schema": SCHEMA,
            "diagnostic_id": frame["diagnostic_id"], "status": "accepted"}


def snapshots(db: sqlite3.Connection, backend_run: str) -> list[dict]:
    rows = db.execute("SELECT r.canonical_body,s.source_public_seq,s.source_journal_seq,g.project,"
        "g.transport_run,g.command_id FROM diagnostic_receipts r JOIN diagnostic_sources s "
        "ON s.source_event_id=r.source_event_id JOIN diagnostic_segments g ON g.command_id=s.command_id "
        "WHERE g.backend_run=? ORDER BY s.source_public_seq", (backend_run,)).fetchall()
    return [{"frame": json.loads(row["canonical_body"]),
             "lineage": {"project": row["project"], "transport_run": row["transport_run"], "command_id": row["command_id"]},
             "source_order": {"public_seq": row["source_public_seq"], "journal_seq": row["source_journal_seq"]}}
            for row in rows]
