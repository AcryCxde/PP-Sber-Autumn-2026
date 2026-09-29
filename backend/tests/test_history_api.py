import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from sqlalchemy import select

import backend.main as main_module
from backend.db import Database
from backend.models import Event, Run
from backend.repository import create_request


class FakeStdout:
    def __init__(self, lines: list[str]):
        self.lines = [f"{line}\n" for line in lines]

    def readline(self) -> str:
        if not self.lines:
            return ""
        return self.lines.pop(0)


class FakeProcess:
    def __init__(self, lines: list[str], exit_code: int = 0):
        self.stdout = FakeStdout(lines)
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
    def __init__(self, database: Database):
        self.app = SimpleNamespace(state=SimpleNamespace(database=database))
        self.messages: list[dict] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(json.loads(message))


class DisconnectingWebSocket(RecordingWebSocket):
    async def send_text(self, message: str) -> None:
        del message
        raise WebSocketDisconnect()


def make_database(tmp_path) -> Database:
    database_path = (tmp_path / "history.db").as_posix()
    return Database(f"sqlite+aiosqlite:///{database_path}")


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

    await main_module.start_claude("Create a file", "session-1")

    arguments = captured["arguments"]
    assert isinstance(arguments, list)
    assert arguments[arguments.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in arguments
    assert arguments[arguments.index("--resume") + 1] == "session-1"
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


def test_history_endpoints_are_empty_for_new_database(tmp_path) -> None:
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        assert client.get("/api/conversations").json() == []
        assert client.get("/api/conversations/missing/messages").status_code == 404
        assert client.get("/api/runs/missing/events").status_code == 404


def test_websocket_request_is_saved_to_history(tmp_path, monkeypatch) -> None:
    async def fake_start_claude(
        prompt: str,
        resume_session_id: str | None = None,
    ) -> FakeProcess:
        assert prompt == "Remember this request"
        assert resume_session_id is None
        return FakeProcess(claude_lines("Saved assistant response"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

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

            assert websocket.receive_json()["type"] == "session"
            message_event = websocket.receive_json()
            assert message_event["type"] == "message"
            assert message_event["content"] == "Saved assistant response"
            result_event = websocket.receive_json()
            assert result_event["type"] == "result"
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


def test_multiple_websocket_requests_resume_same_conversation(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    async def fake_start_claude(
        prompt: str,
        resume_session_id: str | None = None,
    ) -> FakeProcess:
        calls.append((prompt, resume_session_id))
        return FakeProcess(claude_lines(f"Response to {prompt}"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("First turn"))
            first_accepted = websocket.receive_json()
            assert first_accepted["type"] == "run.accepted"
            assert websocket.receive_json()["type"] == "session"
            assert websocket.receive_json()["type"] == "message"
            assert websocket.receive_json()["type"] == "result"

            conversation_id = first_accepted["conversation_id"]
            websocket.send_json(
                run_command("Second turn", conversation_id=conversation_id)
            )
            second_accepted = websocket.receive_json()
            assert second_accepted["type"] == "run.accepted"
            assert second_accepted["conversation_id"] == conversation_id
            assert second_accepted["context_mode"] == "resume"
            assert second_accepted["message"]["sequence"] == 3
            assert websocket.receive_json()["type"] == "session"
            assert websocket.receive_json()["type"] == "message"
            second_result = websocket.receive_json()
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


async def test_disconnected_socket_does_not_cancel_accepted_run(tmp_path, monkeypatch) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    async def fake_start_claude(
        prompt: str,
        resume_session_id: str | None = None,
    ) -> FakeProcess:
        del prompt, resume_session_id
        return FakeProcess(claude_lines("Completed after disconnect"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    request_id = str(uuid4())

    try:
        websocket = DisconnectingWebSocket(database)
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
        assert events[-1].type == "result"
        assert events[-1].payload["assistant_message"]["content"] == "Completed after disconnect"
    finally:
        await database.dispose()


def test_duplicate_client_request_does_not_start_second_process(tmp_path, monkeypatch) -> None:
    calls = 0

    async def fake_start_claude(
        prompt: str,
        resume_session_id: str | None = None,
    ) -> FakeProcess:
        nonlocal calls
        del prompt, resume_session_id
        calls += 1
        return FakeProcess(claude_lines("Done"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))
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


def test_rest_and_websocket_replay_events(tmp_path, monkeypatch) -> None:
    async def fake_start_claude(
        prompt: str,
        resume_session_id: str | None = None,
    ) -> FakeProcess:
        del prompt, resume_session_id
        return FakeProcess(claude_lines("Replay response"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

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
                    type="message",
                    payload={"schema_version": 1, "content": f"event-{seq}"},
                )
                for seq in range(2, 503)
            )
            await session.commit()

        websocket = RecordingWebSocket(database)
        await main_module.handle_subscription(websocket, created.run_id, after_seq=0)

        assert len(websocket.messages) == 503
        assert websocket.messages[0]["type"] == "run.accepted"
        assert websocket.messages[-2]["seq"] == 502
        assert websocket.messages[-1]["type"] == "replay.complete"
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

        websocket = RecordingWebSocket(database)
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
                    type="result",
                    payload={"schema_version": 1, "result": "done"},
                )
            )
            await session.commit()

        await asyncio.wait_for(subscription, timeout=1)
        assert websocket.messages[0]["type"] == "result"
        assert websocket.messages[1]["type"] == "replay.complete"
    finally:
        await database.dispose()
