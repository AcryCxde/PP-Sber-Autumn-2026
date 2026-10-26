import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, WebSocketDisconnect
from fastapi.testclient import TestClient
from sqlalchemy import select

import backend.main as main_module
from backend.api.history import download_event_file
from backend.config import Settings
from backend.db import Database
from backend.models import Event, Run
from backend.repository import accept_run, create_request


class FakeStdout:
    def __init__(self, lines: list[str]):
        self.lines = [f"{line}\n" for line in lines]

    def readline(self) -> str:
        if not self.lines:
            return ""
        return self.lines.pop(0)


class FakeProcess:
    def __init__(
        self,
        lines: list[str],
        exit_code: int = 0,
        stderr_lines: list[str] | None = None,
    ):
        self.stdout = FakeStdout(lines)
        self.stderr = FakeStdout(stderr_lines or [])
        self.exit_code = exit_code
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = self.exit_code
        return self.exit_code

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = self.exit_code

    def kill(self) -> None:
        self.returncode = self.exit_code


class CapturingStdin:
    def __init__(self):
        self.content = ""
        self.closed = False

    def write(self, content: str) -> None:
        self.content += content

    def close(self) -> None:
        self.closed = True


class RecordingWebSocket:
    def __init__(self, database: Database, workspace_root=None):
        root = workspace_root or main_module.APP_SETTINGS.workspace_root
        root.mkdir(parents=True, exist_ok=True)
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                database=database,
                workspace_root=root,
                settings=Settings(
                    workspace_root=root,
                    subscription_window_seconds=0.2,
                    subscription_poll_interval_seconds=0.01,
                ),
                run_tasks=set(),
                run_slot_lock=asyncio.Lock(),
                run_task_count=0,
            )
        )
        self.messages: list[dict] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(json.loads(message))


class SlowRecordingWebSocket(RecordingWebSocket):
    async def send_text(self, message: str) -> None:
        await asyncio.sleep(0.0001)
        await super().send_text(message)


class DisconnectingWebSocket(RecordingWebSocket):
    async def send_text(self, message: str) -> None:
        del message
        raise WebSocketDisconnect()


def make_database(tmp_path) -> Database:
    database_path = (tmp_path / "data" / "history.db").as_posix()
    return Database(f"sqlite+aiosqlite:///{database_path}")


def make_app(
    tmp_path,
    permission_mode="acceptEdits",
    artifact_max_download_bytes=10 * 1024 * 1024,
):
    return main_module.create_app(
        make_database(tmp_path),
        Settings(
            workspace_root=tmp_path / "workspaces",
            claude_permission_mode=permission_mode,
            artifact_max_download_bytes=artifact_max_download_bytes,
            subscription_window_seconds=0.05,
            subscription_poll_interval_seconds=0.01,
        ),
    )


async def test_start_claude_allows_file_edits_without_unsafe_bypass(monkeypatch) -> None:
    captured: dict[str, object] = {}
    stdin = CapturingStdin()

    class ProcessStub:
        def __init__(self):
            self.stdin = stdin

    def fake_popen(arguments, **options):
        captured["arguments"] = arguments
        captured["options"] = options
        return ProcessStub()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)

    await main_module.start_claude(
        "Create a file",
        "/isolated/workspace",
        "session-1",
    )

    arguments = captured["arguments"]
    assert isinstance(arguments, list)
    assert arguments[arguments.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in arguments
    assert arguments[arguments.index("--resume") + 1] == "session-1"
    assert captured["options"]["cwd"] == "/isolated/workspace"
    assert captured["options"]["stderr"] == main_module.subprocess.PIPE
    assert stdin.content == "Create a file"
    assert stdin.closed is True


def claude_lines(response: str, session_id: str = "session-1") -> list[str]:
    return [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session_id,
                "cwd": "/workspace",
                "model": "claude-test",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": response}]},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "session_id": session_id,
                "total_cost_usd": 0.0123,
                "duration_ms": 1250,
                "num_turns": 1,
                "is_error": False,
                "result": response,
            }
        ),
    ]


def tool_result_line(file_path: str, content: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "File written",
                        "tool_use_result": {
                            "type": "file",
                            "file": {
                                "filePath": file_path,
                                "content": content,
                                "numLines": content.count("\n") + 1,
                            },
                        },
                    }
                ]
            },
        }
    )


def write_tool_events(file_path: str, content: str) -> list[str]:
    return [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "write-tool",
                            "name": "Write",
                            "input": {
                                "file_path": file_path,
                                "content": content,
                            },
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "write-tool",
                            "content": f"File created successfully at: {file_path}",
                        }
                    ]
                },
            }
        ),
    ]


def run_command(
    content: str,
    *,
    conversation_id: str | None = None,
    client_request_id: str | None = None,
    context_mode: str = "resume",
) -> dict:
    return {
        "type": "run.create",
        "conversation_id": conversation_id,
        "client_request_id": client_request_id or str(uuid4()),
        "content": content,
        "context_mode": context_mode,
    }


def test_stored_payload_cannot_override_database_event_type() -> None:
    event = Event(
        run_id="run-1",
        seq=1,
        type="legacy.redacted",
        payload={"type": "message.created", "content": "secret"},
    )

    projected = main_module.websocket_event(event, "conversation-1")

    assert projected["type"] == "legacy.redacted"
    assert projected["redacted"] is True
    assert "content" not in projected


def test_app_permission_mode_is_used_for_cli(tmp_path, monkeypatch) -> None:
    observed: list[str | None] = []

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, workspace, resume_session_id
        observed.append(permission_mode)
        return FakeProcess(claude_lines("Done"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path, permission_mode="plan")

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Use plan mode"))
            for _ in range(4):
                websocket.receive_json()

    assert observed == ["plan"]


def test_history_endpoints_are_empty_for_new_database(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/conversations").json() == []
        assert client.get("/api/conversations/missing/messages").status_code == 404
        assert client.get("/api/runs/missing/events").status_code == 404


def test_websocket_request_is_saved_to_history(tmp_path, monkeypatch) -> None:
    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        assert prompt == "Remember this request"
        assert workspace.startswith(str(tmp_path / "workspaces"))
        assert resume_session_id is None
        assert permission_mode == "acceptEdits"
        return FakeProcess(claude_lines("Saved assistant response"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            command = run_command("Remember this request")
            websocket.send_json(command)

            accepted = websocket.receive_json()
            assert accepted["type"] == "run.accepted"
            assert accepted["client_request_id"] == command["client_request_id"]
            assert accepted["message"]["content"] == "Remember this request"
            assert accepted["message"]["sequence"] == 1
            assert accepted["seq"] == 1

            assert websocket.receive_json()["type"] == "run.started"
            message_event = websocket.receive_json()
            assert message_event["type"] == "message.created"
            assert message_event["content"] == "Saved assistant response"
            result_event = websocket.receive_json()
            assert result_event["type"] == "run.completed"
            assert result_event["status"] == "completed"
            assert result_event["session_state"] == "active"
            assert result_event["assistant_message"]["sequence"] == 2

        conversations = client.get("/api/conversations").json()
        assert len(conversations) == 1
        assert conversations[0]["context_state"] == "active"

        messages = client.get(
            f"/api/conversations/{accepted['conversation_id']}/messages"
        ).json()
        assert [(item["sequence"], item["role"], item["kind"]) for item in messages] == [
            (1, "user", "text"),
            (2, "assistant", "text"),
        ]
        conversation_events = client.get(
            f"/api/conversations/{accepted['conversation_id']}/events"
        ).json()
        assert [event["type"] for event in conversation_events] == [
            "run.accepted",
            "run.started",
            "message.created",
            "run.completed",
        ]


def test_workspace_error_finalizes_accepted_run(tmp_path, monkeypatch) -> None:
    def fail_workspace(*args, **kwargs):
        del args, kwargs
        raise main_module.WorkspaceError("invalid workspace")

    monkeypatch.setattr(main_module, "conversation_workspace", fail_workspace)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Cannot prepare workspace"))
            accepted = websocket.receive_json()
            failed = websocket.receive_json()

        replay = client.get(f"/api/runs/{accepted['run_id']}/events").json()

    assert failed["type"] == "run.failed"
    assert failed["error_code"] == "claude_request_failed"
    assert [event["type"] for event in replay] == ["run.accepted", "run.failed"]


def test_result_only_answer_is_persisted_and_published(tmp_path, monkeypatch) -> None:
    lines = [claude_lines("Result-only answer")[0], claude_lines("Result-only answer")[-1]]

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, workspace, resume_session_id, permission_mode
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Return only result"))
            events = [websocket.receive_json() for _ in range(4)]

        conversation_id = events[0]["conversation_id"]
        messages = client.get(
            f"/api/conversations/{conversation_id}/messages"
        ).json()

    assert [event["type"] for event in events] == [
        "run.accepted",
        "run.started",
        "message.created",
        "run.completed",
    ]
    assert events[2]["content"] == "Result-only answer"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Result-only answer"


def test_zero_exit_without_result_has_unknown_outcome(tmp_path, monkeypatch) -> None:
    lines = claude_lines("Partial answer")[:2]

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, workspace, resume_session_id, permission_mode
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Truncated stream"))
            events = [websocket.receive_json() for _ in range(4)]

        conversations = client.get("/api/conversations").json()

    assert [event["type"] for event in events] == [
        "run.accepted",
        "run.started",
        "message.created",
        "run.outcome_unknown",
    ]
    assert events[-1]["error_code"] == "claude_session_state_uncertain"
    assert conversations[0]["context_state"] == "unavailable"


def test_created_file_can_be_downloaded_from_recorded_event(tmp_path, monkeypatch) -> None:
    lines = claude_lines("File created")
    lines[1:1] = write_tool_events("created.txt", "download me")

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, resume_session_id
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Create a file"))
            accepted = websocket.receive_json()
            assert websocket.receive_json()["type"] == "run.started"
            artifact_event = websocket.receive_json()
            assert artifact_event["type"] == "artifact.saved"
            assert artifact_event["path"] == "created.txt"
            assert "content" not in artifact_event
            assert websocket.receive_json()["type"] == "message.created"
            assert websocket.receive_json()["type"] == "run.completed"

        response = client.get(
            f"/api/runs/{accepted['run_id']}/events/{artifact_event['seq']}/file"
        )

    assert response.status_code == 200
    assert response.content == b"download me"
    assert "attachment" in response.headers["content-disposition"]


def test_app_artifact_limit_controls_snapshot_and_download(tmp_path, monkeypatch) -> None:
    lines = claude_lines("File created")
    lines[1:1] = write_tool_events("large.txt", "too large")

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, workspace, resume_session_id, permission_mode
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path, artifact_max_download_bytes=1)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Create a large file"))
            accepted = websocket.receive_json()
            websocket.receive_json()
            artifact_event = websocket.receive_json()
            websocket.receive_json()
            websocket.receive_json()

        response = client.get(
            f"/api/runs/{accepted['run_id']}/events/{artifact_event['seq']}/file"
        )

    assert artifact_event["truncated"] is True
    assert response.status_code == 413


def test_file_download_uses_persisted_snapshot_not_live_path(tmp_path, monkeypatch) -> None:
    live_file = tmp_path / "changed.txt"
    live_file.write_text("live content that must not be read", encoding="utf-8")

    lines = claude_lines("File created")
    lines[1:1] = write_tool_events("changed.txt", "persisted snapshot")

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, resume_session_id
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Create a file"))
            accepted = websocket.receive_json()
            websocket.receive_json()
            tool_event = websocket.receive_json()
            websocket.receive_json()
            websocket.receive_json()

        response = client.get(
            f"/api/runs/{accepted['run_id']}/events/{tool_event['seq']}/file"
        )

    assert response.status_code == 200
    assert response.content == b"persisted snapshot"
    assert response.content != live_file.read_bytes()


async def test_unregistered_legacy_file_snapshot_is_not_downloadable(tmp_path) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Legacy file")

        async with database.session() as session:
            run = await session.get(Run, created.run_id)
            assert run is not None
            run.next_event_seq = 2
            session.add(
                Event(
                    run_id=created.run_id,
                    seq=2,
                    type="tool_result",
                    payload={
                        "schema_version": 1,
                        "file": {
                            "path": "legacy.txt",
                            "content": "legacy content without metadata",
                        },
                    },
                )
            )
            await session.commit()

        async with database.session() as session:
            with pytest.raises(HTTPException) as error:
                await download_event_file(
                    created.run_id,
                    2,
                    session,
                    Settings(workspace_root=tmp_path / "workspaces"),
                )

        assert error.value.status_code == 404
    finally:
        await database.dispose()


def test_multiple_websocket_requests_resume_same_conversation(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        calls.append((prompt, resume_session_id))
        return FakeProcess(claude_lines(f"Response to {prompt}"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("First turn"))
            first_accepted = websocket.receive_json()
            assert first_accepted["type"] == "run.accepted"
            assert websocket.receive_json()["type"] == "run.started"
            assert websocket.receive_json()["type"] == "message.created"
            assert websocket.receive_json()["type"] == "run.completed"

            conversation_id = first_accepted["conversation_id"]
            websocket.send_json(
                run_command("Second turn", conversation_id=conversation_id)
            )
            second_accepted = websocket.receive_json()
            assert second_accepted["type"] == "run.accepted"
            assert second_accepted["conversation_id"] == conversation_id
            assert second_accepted["context_mode"] == "resume"
            assert second_accepted["message"]["sequence"] == 3
            assert websocket.receive_json()["type"] == "run.started"
            assert websocket.receive_json()["type"] == "message.created"
            second_result = websocket.receive_json()
            assert second_result["type"] == "run.completed"
            assert second_result["assistant_message"]["sequence"] == 4

        assert calls == [
            ("First turn", None),
            ("Second turn", "session-1"),
        ]
        conversations = client.get("/api/conversations").json()
        assert len(conversations) == 1
        messages = client.get(
            f"/api/conversations/{conversation_id}/messages"
        ).json()
        assert [item["sequence"] for item in messages] == [1, 2, 3, 4]


def test_resume_failure_on_stderr_requires_context_reset(tmp_path, monkeypatch) -> None:
    calls = 0

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        nonlocal calls
        del prompt, workspace, permission_mode
        calls += 1
        if calls == 1:
            return FakeProcess(claude_lines("First response", session_id="session-1"))
        assert resume_session_id == "session-1"
        return FakeProcess(
            [],
            exit_code=1,
            stderr_lines=["No conversation found for session"],
        )

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("First turn"))
            first = websocket.receive_json()
            for _ in range(3):
                websocket.receive_json()

            websocket.send_json(
                run_command("Resume", conversation_id=first["conversation_id"])
            )
            websocket.receive_json()
            websocket.receive_json()
            failed = websocket.receive_json()

        conversations = client.get("/api/conversations").json()

    assert failed["type"] == "run.failed"
    assert failed["error_code"] == "claude_session_unavailable"
    assert failed["session_state"] == "unavailable"
    assert conversations[0]["context_state"] == "unavailable"


def test_interrupted_resumed_run_invalidates_session_context(tmp_path, monkeypatch) -> None:
    calls = 0

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        nonlocal calls
        del prompt, workspace, permission_mode
        calls += 1
        if calls == 1:
            return FakeProcess(claude_lines("First response", session_id="session-1"))
        assert resume_session_id == "session-1"
        lines = claude_lines("Should not complete", session_id="session-1")
        lines[1:1] = write_tool_events("artifact.txt", "snapshot")
        return FakeProcess(lines)

    async def fail_artifact_save(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    monkeypatch.setattr(main_module, "append_artifact", fail_artifact_save)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("First turn"))
            first = websocket.receive_json()
            for _ in range(3):
                websocket.receive_json()

            websocket.send_json(
                run_command("Create artifact", conversation_id=first["conversation_id"])
            )
            websocket.receive_json()
            websocket.receive_json()
            failed = websocket.receive_json()

        conversations = client.get("/api/conversations").json()

    assert failed["type"] == "run.failed"
    assert failed["error_code"] == "artifact_save_failed"
    assert failed["session_state"] == "unavailable"
    assert conversations[0]["context_state"] == "unavailable"


async def test_disconnected_socket_does_not_cancel_accepted_run(tmp_path, monkeypatch) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, resume_session_id
        return FakeProcess(claude_lines("Completed after disconnect"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    request_id = str(uuid4())

    try:
        websocket = DisconnectingWebSocket(database, tmp_path / "workspaces")
        await main_module.handle_run_create(
            websocket,
            conversation_id=None,
            client_request_id=request_id,
            content="Keep running",
            context_mode="resume",
        )

        async with database.session() as session:
            run = await session.scalar(
                select(Run).where(Run.client_request_id == request_id)
            )
            assert run is not None
            events = list(
                await session.scalars(
                    select(Event)
                    .where(Event.run_id == run.id)
                    .order_by(Event.seq)
                )
            )

        assert run.status == "completed"
        assert events[-1].type == "run.completed"
        assert events[-1].payload["assistant_message"]["content"] == "Completed after disconnect"
    finally:
        await database.dispose()


def test_duplicate_client_request_does_not_start_second_process(tmp_path, monkeypatch) -> None:
    calls = 0

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        nonlocal calls
        del prompt, resume_session_id
        calls += 1
        return FakeProcess(claude_lines("Done"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)
    command = run_command("Once")

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(command)
            accepted = websocket.receive_json()
            websocket.receive_json()
            websocket.receive_json()
            websocket.receive_json()

            websocket.send_json(command)
            replayed_acceptance = websocket.receive_json()
            assert replayed_acceptance["run_id"] == accepted["run_id"]
            assert replayed_acceptance["idempotent_replay"] is True

    assert calls == 1


def test_internal_cli_data_never_reaches_public_history(tmp_path, monkeypatch) -> None:
    secret = "PRIVATE_SENTINEL_42"
    lines = claude_lines("Safe response", session_id="private-session")
    lines[1:1] = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-tool",
                            "name": "Read",
                            "input": {"file_path": f"/host/{secret}/.env"},
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-tool",
                            "content": f"DATABASE_PASSWORD={secret}",
                        }
                    ]
                },
            }
        ),
        secret,
    ]
    result = json.loads(lines[-1])
    result.update(
        {
            "permission_denials": [{"path": f"/host/{secret}"}],
            "subagent_stats": {"secret": secret},
        }
    )
    lines[-1] = json.dumps(result)

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, workspace, resume_session_id
        return FakeProcess(lines, stderr_lines=[f"proxy-token={secret}"])

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Keep internals private"))
            public_events = [websocket.receive_json() for _ in range(4)]

        run_id = public_events[0]["run_id"]
        replay = client.get(f"/api/runs/{run_id}/events").json()

    assert [event["type"] for event in public_events] == [
        "run.accepted",
        "run.started",
        "message.created",
        "run.completed",
    ]
    serialized = json.dumps(public_events) + json.dumps(replay)
    assert secret not in serialized
    assert "private-session" not in serialized
    assert "permission_denials" not in serialized
    assert "subagent_stats" not in serialized
    assert "/host/" not in serialized


def test_failed_or_outside_write_does_not_create_artifact(tmp_path, monkeypatch) -> None:
    lines = claude_lines("No artifact")
    lines[1:1] = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "failed-write",
                            "name": "Write",
                            "input": {
                                "file_path": "failed.txt",
                                "content": "must not persist",
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "outside-write",
                            "name": "Write",
                            "input": {
                                "file_path": str(tmp_path / "outside.txt"),
                                "content": "must not persist either",
                            },
                        },
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "failed-write",
                            "content": "failed",
                            "is_error": True,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "outside-write",
                            "content": "done",
                            "is_error": False,
                        },
                    ]
                },
            }
        ),
    ]

    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, workspace, resume_session_id
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Try unsafe writes"))
            events = [websocket.receive_json() for _ in range(4)]

        run_id = events[0]["run_id"]
        replay = client.get(f"/api/runs/{run_id}/events").json()

    assert all(event["type"] != "artifact.saved" for event in events)
    assert all(event["type"] != "artifact.saved" for event in replay)
    serialized = json.dumps(events) + json.dumps(replay)
    assert str(tmp_path) not in serialized
    assert "must not persist" not in serialized


def test_rest_and_websocket_replay_events(tmp_path, monkeypatch) -> None:
    async def fake_start_claude(
        prompt: str,
        workspace: str,
        resume_session_id: str | None = None,
        *,
        permission_mode: str | None = None,
    ) -> FakeProcess:
        del prompt, resume_session_id
        return FakeProcess(claude_lines("Replay response"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("Replay this"))
            accepted = websocket.receive_json()
            websocket.receive_json()
            websocket.receive_json()
            result = websocket.receive_json()

        run_id = accepted["run_id"]
        response = client.get(
            f"/api/runs/{run_id}/events",
            params={"after_seq": 1, "limit": 2},
        )
        assert [event["seq"] for event in response.json()] == [2, 3]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {"type": "run.subscribe", "run_id": run_id, "after_seq": 2}
            )
            replayed = [websocket.receive_json(), websocket.receive_json()]
            complete = websocket.receive_json()

        assert [event["seq"] for event in replayed] == [3, result["seq"]]
        assert complete["type"] == "replay.complete"
        assert complete["status"] == "completed"


async def test_websocket_replay_reads_all_pages(tmp_path) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Large replay")

        async with database.session() as session:
            run = await session.get(Run, created.run_id)
            assert run is not None
            run.status = "completed"
            run.next_event_seq = 502
            session.add_all(
                Event(
                    run_id=created.run_id,
                    seq=seq,
                    type="message.created",
                    payload={"schema_version": 1, "content": f"event-{seq}"},
                )
                for seq in range(2, 503)
            )
            await session.commit()

        websocket = RecordingWebSocket(database, tmp_path / "workspaces")
        await main_module.handle_subscription(websocket, created.run_id, after_seq=0)

        assert len(websocket.messages) == 503
        assert websocket.messages[0]["type"] == "run.accepted"
        assert websocket.messages[-2]["seq"] == 502
        assert websocket.messages[-1]["type"] == "replay.complete"
    finally:
        await database.dispose()


async def test_run_task_admission_limit_is_enforced(tmp_path) -> None:
    database = make_database(tmp_path)
    websocket = RecordingWebSocket(database, tmp_path / "workspaces")
    websocket.app.state.settings.max_concurrent_runs = 1
    gate = asyncio.Event()

    async def hold_slot() -> None:
        await gate.wait()

    assert await main_module.start_run_task(websocket, hold_slot()) is True
    assert await main_module.start_run_task(websocket, hold_slot()) is False
    assert websocket.app.state.run_task_count == 1

    gate.set()
    await asyncio.gather(*websocket.app.state.run_tasks)
    assert websocket.app.state.run_task_count == 0
    await database.dispose()


async def test_idempotent_retry_is_allowed_at_run_capacity(tmp_path) -> None:
    database = make_database(tmp_path)
    await database.create_schema()
    request_id = str(uuid4())

    try:
        async with database.session() as session:
            accepted = await accept_run(
                session,
                conversation_id=None,
                client_request_id=request_id,
                content="Existing request",
            )

        websocket = RecordingWebSocket(database, tmp_path / "workspaces")
        websocket.app.state.settings.max_concurrent_runs = 1
        websocket.app.state.run_task_count = 1
        await main_module.dispatch_websocket_message(
            websocket,
            json.dumps(
                run_command(
                    "Existing request",
                    conversation_id=accepted.conversation_id,
                    client_request_id=request_id,
                )
            ),
        )
        await asyncio.gather(*websocket.app.state.run_tasks)

        assert websocket.messages[0]["type"] == "run.accepted"
        assert websocket.messages[0]["idempotent_replay"] is True
        assert websocket.app.state.run_task_count == 1
    finally:
        await database.dispose()


async def test_blank_legacy_websocket_request_is_rejected(tmp_path) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    try:
        websocket = RecordingWebSocket(database, tmp_path / "workspaces")
        await main_module.dispatch_websocket_message(websocket, "   \n")

        async with database.session() as session:
            runs = list(await session.scalars(select(Run)))

        assert websocket.messages == [
            {
                "type": "command.error",
                "request_type": "run.create",
                "client_request_id": None,
                "conversation_id": None,
                "code": "invalid_command",
                "message": "Пустой запрос не допускается.",
                "retryable": False,
            }
        ]
        assert runs == []
    finally:
        await database.dispose()


async def test_subscription_deadline_applies_to_full_backlog_pages(tmp_path) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Large bounded replay")
            run = await session.get(Run, created.run_id)
            assert run is not None
            run.status = "completed"
            run.next_event_seq = 1002
            session.add_all(
                Event(
                    run_id=created.run_id,
                    seq=seq,
                    type="message.created",
                    payload={"schema_version": 1, "content": f"event-{seq}"},
                )
                for seq in range(2, 1003)
            )
            await session.commit()

        websocket = SlowRecordingWebSocket(database, tmp_path / "workspaces")
        websocket.app.state.settings.subscription_window_seconds = 0.01
        await main_module.handle_subscription(websocket, created.run_id, after_seq=0)

        complete = websocket.messages[-1]
        assert complete["type"] == "replay.complete"
        assert complete["terminal"] is False
        assert complete["last_seq"] == 500
        assert len(websocket.messages) == 501
    finally:
        await database.dispose()


async def test_nonterminal_subscription_is_bounded_and_does_not_block_commands(
    tmp_path,
) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Bounded replay")
            run = await session.get(Run, created.run_id)
            assert run is not None
            run.status = "running"
            await session.commit()

        websocket = RecordingWebSocket(database, tmp_path / "workspaces")
        websocket.app.state.settings.subscription_window_seconds = 0.03
        await main_module.dispatch_websocket_message(
            websocket,
            json.dumps(
                {
                    "type": "run.subscribe",
                    "run_id": created.run_id,
                    "after_seq": 1,
                }
            ),
        )
        await main_module.dispatch_websocket_message(
            websocket,
            json.dumps({"type": "unknown.command"}),
        )

        assert any(message["type"] == "command.error" for message in websocket.messages)
        subscriptions = websocket._gateway_subscriptions
        await asyncio.gather(*subscriptions.values())
        complete = websocket.messages[-1]
        assert complete["type"] == "replay.complete"
        assert complete["terminal"] is False
        assert complete["status"] == "running"
    finally:
        await database.dispose()


async def test_websocket_replay_waits_for_running_run_to_finish(tmp_path) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Running replay")
            run = await session.get(Run, created.run_id)
            assert run is not None
            run.status = "running"
            await session.commit()

        websocket = RecordingWebSocket(database, tmp_path / "workspaces")
        subscription = asyncio.create_task(
            main_module.handle_subscription(websocket, created.run_id, after_seq=1)
        )
        await asyncio.sleep(0.05)
        assert not subscription.done()

        async with database.session() as session:
            run = await session.get(Run, created.run_id)
            assert run is not None
            run.status = "completed"
            run.next_event_seq = 2
            session.add(
                Event(
                    run_id=created.run_id,
                    seq=2,
                    type="run.completed",
                    payload={"schema_version": 1, "status": "completed"},
                )
            )
            await session.commit()

        await asyncio.wait_for(subscription, timeout=1)
        assert websocket.messages[0]["type"] == "run.completed"
        assert websocket.messages[1]["type"] == "replay.complete"
    finally:
        await database.dispose()
