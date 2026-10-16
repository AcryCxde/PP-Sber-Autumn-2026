"""Durable transport journal for protocol v1; NOT the artifact storage service.

Only gateway code calls commands()/message()/answer()/confirm_saved(). No new
public browser API is introduced. Keep the database outside project volumes.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import UTC, datetime
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


TERMINAL = {"completed", "failed", "outcome_unknown"}
ID = re.compile(r"[0-9A-Za-z_-]{1,64}\Z")
EVENT_FIELDS = {
    "run.started": set(),
    "role.started": {"role_id", "role_instance_id", "title"},
    "role.completed": {"role_id", "role_instance_id", "outcome"},
    "message.created": {"role_id", "text"},
    "facilitator.question": {"question_id", "questions"},
    "user.answer.accepted": {"question_id", "answers"},
    "progress.updated": {"stage", "attempt", "context_restored"},
    "usage.reported": {"input_tokens", "output_tokens", "cache_creation_input_tokens",
                       "cache_read_input_tokens", "num_turns", "duration_ms",
                       "duration_api_ms", "cost_usd"},
    "artifact.created": {"artifact_id", "path", "commit_sha", "role_id", "size_bytes", "sha256"},
    "run.completed": set(),
    "run.failed": {"code", "retryable"},
    "run.outcome_unknown": {"code"},
}


class TransportError(ValueError):
    """Stable errors for the embedding gateway, never SDK/internal data."""


def _id(value: Any) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise TransportError("invalid_id")
    return value


def _path(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512
            or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(p in {"", ".", "..", ".git", ".runner"} for p in value.split("/"))):
        raise TransportError("invalid_path")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _event(project: str, run: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"event_id": str(uuid4()), "project_id": project, "run_id": run,
            "occurred_at": _now(), "type": kind, "payload": payload}


def validate_event(event: Any, project: str) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) != {
        "event_id", "project_id", "run_id", "occurred_at", "type", "payload"
    }:
        raise TransportError("invalid_event")
    if event["project_id"] != project:
        raise TransportError("project_mismatch")
    _id(event["run_id"])
    try:
        UUID(event["event_id"])
    except (ValueError, TypeError, AttributeError):
        raise TransportError("invalid_event_id")
    try:
        stamp = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise TransportError("invalid_timestamp") from None
    kind, payload = event["type"], event["payload"]
    if not isinstance(kind, str) or kind not in EVENT_FIELDS or not isinstance(payload, dict):
        raise TransportError("invalid_event_type")
    if set(payload) - EVENT_FIELDS[kind]:
        raise TransportError("private_or_unknown_event_fields")
    optional = {"artifact.created": {"size_bytes", "sha256"},
                "progress.updated": {"attempt", "context_restored"}}
    if EVENT_FIELDS[kind] - optional.get(kind, set()) - set(payload):
        raise TransportError("missing_event_fields")
    for field in ("role_id", "title"):
        if field in payload and payload[field] is not None and not isinstance(payload[field], str):
            raise TransportError("invalid_label")
    for field in ("code", "stage", "outcome", "question_id"):
        if field in payload and not isinstance(payload[field], str):
            raise TransportError("invalid_string_field")
    if "attempt" in payload and (type(payload["attempt"]) is not int or payload["attempt"] < 1):
        raise TransportError("invalid_attempt")
    if "context_restored" in payload and type(payload["context_restored"]) is not bool:
        raise TransportError("invalid_context_restored")
    if kind == "facilitator.question":
        if not isinstance(payload.get("question_id"), str):
            raise TransportError("invalid_question")
        questions = payload.get("questions")
        if not isinstance(questions, list) or not questions:
            raise TransportError("invalid_question")
        for q in questions:
            if (not isinstance(q, dict) or not isinstance(q.get("text"), str)
                    or not q["text"] or set(q) - {
                        "text", "header", "options", "multi_select", "allow_custom"}):
                raise TransportError("invalid_question")
            if not isinstance(q.get("options"), list):
                raise TransportError("invalid_question")
            if (set(q) != {"text", "header", "options", "multi_select", "allow_custom"}
                    or q["header"] is not None and not isinstance(q["header"], str)
                    or type(q["multi_select"]) is not bool or type(q["allow_custom"]) is not bool):
                raise TransportError("invalid_question")
            for option in q["options"]:
                if (not isinstance(option, dict) or not isinstance(option.get("label"), str)
                        or set(option) != {"label", "description"}
                        or option["description"] is not None and not isinstance(option["description"], str)):
                    raise TransportError("invalid_question")
    if kind == "artifact.created":
        _path(payload.get("path"))
        if not all(isinstance(payload.get(k), str) and payload[k]
                   for k in ("artifact_id", "commit_sha")):
            raise TransportError("invalid_artifact")
        if "size_bytes" in payload and (
                type(payload["size_bytes"]) is not int or payload["size_bytes"] < 0):
            raise TransportError("invalid_artifact")
        if "sha256" in payload and not re.fullmatch(r"[0-9a-f]{64}", str(payload["sha256"])):
            raise TransportError("invalid_artifact")
    if kind == "message.created" and not isinstance(payload.get("text"), str):
        raise TransportError("invalid_message")
    if kind == "run.outcome_unknown" and payload != {"code": "state_corrupt"}:
        raise TransportError("gateway_only_unknown_code")
    if kind == "run.failed" and (payload.get("code") not in {
            "result_error", "checkpoint_failed", "resume_limit", "disk_full"}
            or type(payload.get("retryable")) is not bool):
        raise TransportError("invalid_failure")
    if kind == "usage.reported" and (set(payload) != EVENT_FIELDS[kind] or any(
            v is not None and (type(v) not in {int, float} or not math.isfinite(v) or v < 0)
            for v in payload.values())):
        raise TransportError("invalid_usage")
    if kind == "progress.updated" and payload.get("stage") not in {
            "interrupted", "recovering", "working"}:
        raise TransportError("invalid_progress")
    if kind == "progress.updated" and payload["stage"] == "working" and (
            type(payload.get("attempt")) is not int or payload["attempt"] < 1
            or type(payload.get("context_restored")) is not bool):
        raise TransportError("invalid_progress")
    if kind == "user.answer.accepted" and (
            not isinstance(payload["question_id"], str) or not isinstance(payload["answers"], dict)
            or any(not isinstance(k, str) or not isinstance(v, str)
                   for k, v in payload["answers"].items())):
        raise TransportError("invalid_answers")
    if kind.startswith("role.") and (
            not isinstance(payload.get("role_id"), str)
            or not isinstance(payload.get("role_instance_id"), str)):
        raise TransportError("invalid_role")
    if kind == "role.completed" and payload.get("outcome") not in {"completed", "failed"}:
        raise TransportError("invalid_role")
    return event


class RunnerTransport:
    """One process owns this SQLite file (use the gateway's own worker routing).

    Synchronous, short SQLite transactions; no model calls/network while locked.
    Embedders should offload to a worker for high-volume ingestion.
    """

    def __init__(self, database: str | Path):
        self.database = str(database)
        with self._db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
                seen REAL, phase TEXT, health TEXT);
              CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, project TEXT NOT NULL,
                client_request TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                question TEXT, completion TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS transport_one_active_project
                ON runs(project) WHERE status NOT IN ('completed','failed','outcome_unknown');
              CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, run TEXT NOT NULL,
                body TEXT NOT NULL, ack TEXT);
              CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, project TEXT NOT NULL, run TEXT NOT NULL,
                seq INTEGER NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL,
                UNIQUE(run,seq));
              CREATE TABLE IF NOT EXISTS artifacts (
                run TEXT NOT NULL, id TEXT NOT NULL, created TEXT NOT NULL, saved TEXT,
                PRIMARY KEY(run,id));
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _run(db: sqlite3.Connection, project: str, run: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM runs WHERE id=? AND project=?", (run, project)).fetchone()
        if row is None:
            raise TransportError("unknown_run")
        return row

    @staticmethod
    def _append(db: sqlite3.Connection, body: dict[str, Any], source: str) -> None:
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run=?",
                         (body["run_id"],)).fetchone()[0]
        public = {"schema": "events.v0", **body, "seq": seq}
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (
            body["event_id"], body["project_id"], body["run_id"], seq,
            json.dumps(public, ensure_ascii=False), source))

    def hello(self, project: str) -> int:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO projects(id) VALUES(?)", (project,))
            db.execute("UPDATE projects SET seen=? WHERE id=?", (time.time(), project))
            return db.execute("SELECT cursor FROM projects WHERE id=?", (project,)).fetchone()[0]

    def heartbeat(self, project: str, frame: dict[str, Any]) -> None:
        if (not isinstance(frame.get("phase"), str)
                or frame["phase"] not in {"idle", "working", "awaiting_answer", "failed"}
                or not isinstance(frame.get("health"), str)
                or frame["health"] not in {"ok", "stalled", "crashed"}
                or type(frame.get("last_progress_at")) not in {int, float}
                or not math.isfinite(frame["last_progress_at"])):
            raise TransportError("invalid_heartbeat")
        with self._db() as db:
            db.execute("UPDATE projects SET seen=?,phase=?,health=? WHERE id=?",
                       (time.time(), frame["phase"], frame["health"], project))

    def message(self, project: str, run_id: str, client_request_id: str, text: str,
                *, roles: list[str] | None = None, stage: str | None = None,
                input_files: list[str] | None = None) -> dict[str, Any]:
        _id(run_id)
        if (not isinstance(project, str) or not project or not isinstance(text, str) or not text
                or not isinstance(client_request_id, str) or not client_request_id):
            raise TransportError("invalid_message")
        if roles is not None and (not isinstance(roles, list) or len(roles) > 32 or any(
                not isinstance(r, str) or not 1 <= len(r) <= 128 for r in roles)):
            raise TransportError("invalid_roles")
        if stage is not None and (not isinstance(stage, str) or not 1 <= len(stage) <= 128):
            raise TransportError("invalid_stage")
        if input_files is not None:
            if not isinstance(input_files, list) or len(input_files) > 64:
                raise TransportError("invalid_input_files")
            for path in input_files:
                _path(path)
        command = {"v": 1, "type": "command", "id": run_id, "kind": "message", "text": text}
        for name, value in (("roles", roles), ("stage", stage), ("input_files", input_files)):
            if value is not None:
                command[name] = value
        with self._db() as db:
            old = db.execute("SELECT * FROM runs WHERE client_request=?", (client_request_id,)).fetchone()
            if old is not None:
                saved = db.execute("SELECT body FROM commands WHERE id=?", (old["id"],)).fetchone()
                if old["project"] != project or json.loads(saved[0]) != command:
                    raise TransportError("idempotency_conflict")
                return json.loads(saved[0])
            db.execute("INSERT OR IGNORE INTO projects(id) VALUES(?)", (project,))
            try:
                db.execute("INSERT INTO runs VALUES(?,?,?,'queued',NULL,NULL)",
                           (run_id, project, client_request_id))
                db.execute("INSERT INTO commands VALUES(?,?,?,?,NULL)",
                           (run_id, project, run_id, json.dumps(command)))
            except sqlite3.IntegrityError:
                raise TransportError("active_run_or_id_conflict") from None
            self._append(db, _event(project, run_id, "run.accepted",
                                   {"client_request_id": client_request_id}), "gateway")
        return command

    def answer(self, project: str, run_id: str, command_id: str,
               question_id: str, answers: dict[str, str]) -> dict[str, Any]:
        _id(command_id)
        command = {"v": 1, "type": "command", "id": command_id,
                   "kind": "fork_answer", "fork_id": question_id, "answers": answers}
        if not isinstance(answers, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in answers.items()):
            raise TransportError("invalid_answers")
        with self._db() as db:
            old = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if old is not None:
                if old["project"] != project or old["run"] != run_id or json.loads(old["body"]) != command:
                    raise TransportError("idempotency_conflict")
                return command
            run = self._run(db, project, run_id)
            question = json.loads(run["question"]) if run["question"] else {}
            if run["status"] != "waiting_for_input" or question.get("question_id") != question_id:
                raise TransportError("unknown_fork")
            if set(answers) != {q["text"] for q in question["questions"]}:
                raise TransportError("answers_mismatch")
            db.execute("INSERT INTO commands VALUES(?,?,?,?,NULL)",
                       (command_id, project, run_id, json.dumps(command)))
        return command

    def commands(self, project: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT c.body FROM commands c JOIN runs r ON c.run=r.id "
                "WHERE c.project=? AND c.ack IS NULL "
                "AND r.status NOT IN ('completed','failed','outcome_unknown') ORDER BY c.rowid", (project,))]

    def ack(self, project: str, frame: dict[str, Any]) -> None:
        if (not isinstance(frame.get("status"), str)
                or frame["status"] not in {"accepted", "rejected"}
                or not isinstance(frame.get("id"), str)
                or "reason" in frame and not isinstance(frame["reason"], str)):
            raise TransportError("invalid_ack")
        receipt = {k: frame[k] for k in ("id", "status", "reason") if k in frame}
        with self._db() as db:
            row = db.execute("SELECT * FROM commands WHERE id=? AND project=?",
                             (frame.get("id"), project)).fetchone()
            if row is None:
                raise TransportError("unknown_command")
            if row["ack"] is not None and json.loads(row["ack"]) != receipt:
                raise TransportError("ack_conflict")
            db.execute("UPDATE commands SET ack=? WHERE id=?", (json.dumps(receipt), row["id"]))

    def ingest(self, project: str, journal_seq: int, body: Any) -> bool:
        if type(journal_seq) is not int or journal_seq < 1:
            raise TransportError("invalid_journal_seq")
        event = validate_event(body, project)
        with self._db() as db:
            run = self._run(db, project, event["run_id"])
            seen = db.execute("SELECT body,source FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            source = json.dumps(event, sort_keys=True, ensure_ascii=False)
            if seen:
                if seen["source"] != source:
                    raise TransportError("event_id_conflict")
                return False
            if run["completion"]:
                if json.loads(run["completion"])["event_id"] == event["event_id"]:
                    if json.loads(run["completion"]) != event:
                        raise TransportError("event_id_conflict")
                    return False
                raise TransportError("event_after_completion_request")
            if run["status"] in TERMINAL:
                raise TransportError("terminal_run")
            kind, payload = event["type"], event["payload"]
            if kind == "user.answer.accepted":
                question = json.loads(run["question"]) if run["question"] else {}
                if (question.get("question_id") != payload.get("question_id")
                        or set(payload["answers"]) != {q["text"] for q in question.get("questions", [])}):
                    raise TransportError("unknown_fork")
            if kind == "artifact.created":
                db.execute("INSERT INTO artifacts VALUES(?,?,?,NULL)",
                           (run["id"], payload["artifact_id"], json.dumps(payload)))
            if kind == "run.completed":
                db.execute("UPDATE runs SET completion=? WHERE id=?", (json.dumps(event), run["id"]))
                self._finish_if_saved(db, project, run["id"])
            else:
                self._append(db, event, source)
                status = run["status"]
                if kind in {"run.started", "user.answer.accepted", "progress.updated"}:
                    status = "running"
                if kind == "facilitator.question":
                    status = "waiting_for_input"
                    db.execute("UPDATE runs SET question=? WHERE id=?", (json.dumps(payload), run["id"]))
                if kind.startswith("run.") and kind[4:] in TERMINAL:
                    status = kind[4:]
                db.execute("UPDATE runs SET status=? WHERE id=?", (status, run["id"]))
            db.execute("UPDATE projects SET cursor=MAX(cursor,?) WHERE id=?", (journal_seq, project))
        return True

    def _finish_if_saved(self, db: sqlite3.Connection, project: str, run: str) -> None:
        row = self._run(db, project, run)
        missing = db.execute("SELECT COUNT(*) FROM artifacts WHERE run=? AND saved IS NULL", (run,)).fetchone()[0]
        if row["completion"] and not missing and row["status"] not in TERMINAL:
            event = json.loads(row["completion"])
            self._append(db, event, json.dumps(event, sort_keys=True, ensure_ascii=False))
            db.execute("UPDATE runs SET status='completed' WHERE id=?", (run,))

    def confirm_saved(self, project: str, run: str, artifact: str,
                      receipt: dict[str, Any]) -> None:
        """Trusted gateway/storage-adapter ONLY, after durable byte/hash readback.

        This transport cannot produce or independently verify a storage receipt.
        Do not expose this method as a browser or runner command.
        """
        if (set(receipt) != {"artifact_id", "version_id", "size_bytes", "download"}
                or receipt.get("artifact_id") != artifact
                or not isinstance(receipt.get("version_id"), str) or not receipt["version_id"]
                or type(receipt.get("size_bytes")) is not int or receipt["size_bytes"] < 0
                or not isinstance(receipt.get("download"), str) or not receipt["download"]):
            raise TransportError("invalid_storage_receipt")
        with self._db() as db:
            state = self._run(db, project, run)
            row = db.execute("SELECT * FROM artifacts WHERE run=? AND id=?", (run, artifact)).fetchone()
            if row is None:
                raise TransportError("unknown_artifact")
            if row["saved"]:
                if json.loads(row["saved"]) != receipt:
                    raise TransportError("storage_receipt_conflict")
                return
            if state["status"] in TERMINAL:
                raise TransportError("terminal_run")
            metadata = json.loads(row["created"])
            if "size_bytes" in metadata and metadata["size_bytes"] != receipt["size_bytes"]:
                raise TransportError("artifact_size_mismatch")
            db.execute("UPDATE artifacts SET saved=? WHERE run=? AND id=?", (json.dumps(receipt), run, artifact))
            self._append(db, _event(project, run, "artifact.saved", receipt), "gateway")
            self._finish_if_saved(db, project, run)

    def events(self, project: str, run: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._db() as db:
            self._run(db, project, run)
            return [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM events WHERE run=? AND seq>? ORDER BY seq", (run, after_seq))]

    def status(self, project: str, run: str) -> dict[str, Any]:
        with self._db() as db:
            row = self._run(db, project, run)
            rejected = [json.loads(r[0]) for r in db.execute(
                "SELECT ack FROM commands WHERE project=? AND run=? AND ack IS NOT NULL",
                (project, run)) if json.loads(r[0])["status"] == "rejected"]
            return {"run_id": run, "status": row["status"],
                    "awaiting_storage": bool(row["completion"]) and row["status"] not in TERMINAL,
                    "delivery_blocked": bool(rejected), "rejected_commands": rejected}

    def ack_receipt(self, project: str, command_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT ack FROM commands WHERE id=? AND project=?", (command_id, project)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def expire(self, *, now: float | None = None, timeout: float = 30) -> list[str]:
        """Only actual expired registered connections, never missing storage config."""
        expired = []
        with self._db() as db:
            for row in db.execute("SELECT r.* FROM runs r JOIN projects p ON r.project=p.id "
                                  "WHERE p.seen IS NOT NULL AND p.seen<? "
                                  "AND r.status NOT IN ('completed','failed','outcome_unknown')",
                                  ((time.time() if now is None else now) - timeout,)).fetchall():
                if row["completion"]:
                    continue  # runner finished; no synthetic storage timeout policy
                self._append(db, _event(row["project"], row["id"], "run.outcome_unknown",
                                       {"code": "heartbeat_lost"}), "gateway")
                db.execute("UPDATE runs SET status='outcome_unknown' WHERE id=?", (row["id"],))
                expired.append(row["id"])
        return expired
