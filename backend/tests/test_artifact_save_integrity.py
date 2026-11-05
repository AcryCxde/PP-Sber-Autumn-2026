"""Artifact integrity with the existing FakeDocker/FakeRunner, real SQLite and HTTP downloads."""

import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import backend.runner_executor as executor
from backend.models import Artifact
from backend.tests.fake_docker import FakeDocker
from backend.tests.test_runner_executor import (
    FakeRunner, make_runner_app, receive_until_terminal, run_command,
)


@pytest.fixture
def artifact_run(tmp_path, monkeypatch):
    def execute(data, *, limit=32, metadata=None, fault=None, replay=False, restart=False):
        docker = FakeDocker()
        app = make_runner_app(tmp_path, docker)
        app.state.settings.artifact_max_download_bytes = limit
        transport = app.state.runner_transport
        receipts = []
        durable_at_confirmation = []
        original_confirm = transport.confirm_saved

        def confirm(*args):
            receipts.append(args[-1])
            # An independent connection cannot see an uncommitted snapshot.
            with sqlite3.connect(tmp_path / "data" / "history.db") as db:
                snapshot = db.execute("SELECT content,size_bytes,truncated FROM artifacts").fetchone()
            durable_at_confirmation.append(
                snapshot is not None and snapshot[0] is not None
                and snapshot[0].encode("utf-8") == data
                and snapshot[1] == len(data) and not snapshot[2]
            )
            return original_confirm(*args)

        monkeypatch.setattr(transport, "confirm_saved", confirm)
        if fault == "read":
            async def unreadable(*args):
                raise OSError("synthetic read failure")
            monkeypatch.setattr(app.state.runner_manager, "read_file", unreadable)
        if fault == "write":
            async def unwritable(*args, **kwargs):
                raise OSError("synthetic write failure")
            monkeypatch.setattr(executor, "append_artifact", unwritable)
        if fault == "commit":
            original_commit = AsyncSession.commit

            async def fail_artifact_commit(session):
                if await session.scalar(select(Artifact.id).limit(1)) is not None:
                    raise OSError("synthetic commit failure")
                return await original_commit(session)

            monkeypatch.setattr(AsyncSession, "commit", fail_artifact_commit)
        if fault == "readback-read":
            @event.listens_for(app.state.database.engine.sync_engine, "before_cursor_execute")
            def fail_readback(connection, cursor, statement, parameters, context, many):
                if statement.lstrip().startswith("SELECT artifacts.content, artifacts.size_bytes"):
                    raise OSError("synthetic database readback failure")
        if fault in {"content", "size", "missing", "truncated", "path"}:
            @event.listens_for(app.state.database.engine.sync_engine, "after_cursor_execute")
            def damage_snapshot(connection, cursor, statement, parameters, context, many):
                if not statement.lstrip().upper().startswith("INSERT INTO ARTIFACTS "):
                    return
                if fault == "missing":
                    cursor.execute("DELETE FROM artifacts")
                else:
                    column, value = {
                        "content": ("content", "corrupted"),
                        "size": ("size_bytes", 999),
                        "truncated": ("truncated", 1),
                        "path": ("path", "different.txt"),
                    }[fault]
                    cursor.execute(f"UPDATE artifacts SET {column} = ?", (value,))

        payload = {
            "artifact_id": "a1", "path": "result.txt", "commit_sha": "abc123",
            "role_id": None, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if metadata:
            payload.update(metadata)

        def script(command, container):
            container.files["/workspace/project/result.txt"] = data
            return [("run.started", {}), ("artifact.created", payload), ("run.completed", {})]

        command = run_command("Create result.txt")
        repeated = None
        downloaded = None
        with TestClient(app) as client:
            runner = FakeRunner(transport, docker, script)
            runner.start()
            try:
                with client.websocket_connect("/ws") as websocket:
                    websocket.send_json(command)
                    events = receive_until_terminal(websocket)
                    if replay:
                        websocket.send_json(command)
                        # A replay acknowledges the existing Run; history is fetched separately.
                        repeated = websocket.receive_json()
                if receipts:
                    response = client.get(receipts[-1]["download"])
                    downloaded = (response.status_code, response.content)
            finally:
                runner.halt.set()
                runner.join(timeout=3)
                assert not runner.is_alive()
            project = events[0]["conversation_id"].lower()
            transport_events = transport.events(project, events[0]["run_id"])
            commands = len(runner.received)

        restored = None
        if restart and receipts:
            restored_app = make_runner_app(tmp_path, docker)
            restored_app.state.settings.artifact_max_download_bytes = limit
            with TestClient(restored_app) as client:
                response = client.get(receipts[-1]["download"])
                restored = (response.status_code, response.content)
        with sqlite3.connect(tmp_path / "data" / "history.db") as db:
            artifact_count = db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            saved_count = db.execute("SELECT COUNT(*) FROM events WHERE type='artifact.saved'").fetchone()[0]
        return {
            "events": events, "receipts": receipts, "transport": transport_events,
            "download": downloaded, "restored": restored, "replay": repeated,
            "artifacts": artifact_count, "saved_events": saved_count, "commands": commands,
            "durable_at_confirmation": durable_at_confirmation,
        }
    return execute


@pytest.mark.parametrize("data", [b"plain\n", "Привет\r\nмир\r\n".encode(),
                                      b"\xef\xbb\xbftext\r\n", b"", ("я" * 16).encode()],
                         ids=["ascii", "utf8-crlf", "bom", "empty", "exact-byte-limit"])
def test_saved_bytes_match_receipt_and_survive_restart(artifact_run, data):
    result = artifact_run(data, restart=True)
    assert result["events"][-1]["type"] == "run.completed"
    assert result["artifacts"] == result["saved_events"] == 1
    assert len(result["receipts"]) == 1
    assert result["durable_at_confirmation"] == [True]
    assert result["receipts"][0]["size_bytes"] == len(data)
    assert result["download"] == result["restored"] == (200, data)
    assert hashlib.sha256(result["download"][1]).digest() == hashlib.sha256(data).digest()


@pytest.mark.parametrize(("data", "metadata", "fault"), [
    (b"x" * 33, None, None),
    (b"\xff\xfe\x00", None, None),
    (b"ok", {"sha256": "0" * 64}, None),
    (b"ok", {"size_bytes": 999}, None),
    (b"ok", None, "read"),
    (b"ok", None, "write"),
    (b"ok", None, "commit"),
    (b"ok", None, "readback-read"),
    (b"ok", None, "content"),
    (b"ok", None, "size"),
    (b"ok", None, "missing"),
    (b"ok", None, "truncated"),
    (b"ok", None, "path"),
], ids=["oversize", "invalid-utf8", "wrong-hash", "wrong-size", "read-failure", "write-failure",
        "commit-failure", "readback-query-failure",
        "readback-content", "readback-size", "readback-missing", "readback-truncated", "readback-path"])
def test_invalid_or_unpersisted_artifact_never_reports_success(artifact_run, data, metadata, fault):
    result = artifact_run(data, metadata=metadata, fault=fault)
    assert result["events"][-1]["type"] == "run.failed"
    assert result["events"][-1]["error_code"] == "artifact_save_failed"
    assert result["receipts"] == []
    assert result["artifacts"] == result["saved_events"] == 0
    assert not {"artifact.saved", "run.completed"}.intersection(e["type"] for e in result["events"])
    assert not {"artifact.saved", "run.completed"}.intersection(e["type"] for e in result["transport"])


def test_repeated_request_does_not_save_or_confirm_twice(artifact_run):
    result = artifact_run(b"same bytes", replay=True)
    assert result["events"][-1]["type"] == "run.completed"
    assert result["replay"]["type"] == "run.accepted" and result["replay"]["idempotent_replay"]
    assert result["events"][0]["run_id"] == result["replay"]["run_id"]
    assert result["commands"] == 1
    assert result["artifacts"] == result["saved_events"] == len(result["receipts"]) == 1
