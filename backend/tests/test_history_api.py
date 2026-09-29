import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.db import Database
from backend.models import Event
from backend.repository import create_request


class FakeStdout:
    def __init__(self, lines: list[str]):
        self.lines = [f"{line}\n".encode() for line in lines]

    async def readline(self) -> bytes:
        if not self.lines:
            return b""
        return self.lines.pop(0)


class FakeProcess:
    def __init__(self, lines: list[str], exit_code: int = 0):
        self.stdout = FakeStdout(lines)
        self.exit_code = exit_code
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = self.exit_code
        return self.exit_code

    def terminate(self) -> None:
        self.returncode = self.exit_code

    def kill(self) -> None:
        self.returncode = self.exit_code


class RecordingWebSocket:
    def __init__(self, database: Database):
        self.app = SimpleNamespace(
            state=SimpleNamespace(database=database),
        )
        self.messages: list[dict] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(json.loads(message))


def make_database(tmp_path) -> Database:
    database_path = (tmp_path / "history.db").as_posix()
    return Database(f"sqlite+aiosqlite:///{database_path}")


def result_lines(response: str = "Done") -> list[str]:
    return [
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": response,
            }
        )
    ]


def assert_event_context(event: dict, history: dict, seq: int) -> None:
    assert event["conversation_id"] == history["conversation_id"]
    assert event["run_id"] == history["run_id"]
    assert event["seq"] == seq
    assert event["schema_version"] == 1


def test_history_endpoints_are_empty_for_new_database(tmp_path) -> None:
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/conversations")
        assert response.status_code == 200
        assert response.json() == []

        response = client.get("/api/conversations/missing/messages")
        assert response.status_code == 404

        response = client.get("/api/runs/missing/events")
        assert response.status_code == 404


def test_websocket_request_is_saved_to_history(tmp_path, monkeypatch) -> None:
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
                "cwd": "/workspace",
                "model": "claude-test",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Saved assistant response",
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "session_id": "session-1",
                "total_cost_usd": 0.0123,
                "duration_ms": 1250,
                "num_turns": 1,
                "is_error": False,
                "result": "Saved assistant response",
            }
        ),
    ]

    async def fake_start_claude(prompt: str) -> FakeProcess:
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("Remember this request")

            history_event = websocket.receive_json()
            assert history_event["type"] == "history"
            assert_event_context(history_event, history_event, 1)

            session_event = websocket.receive_json()
            assert session_event["type"] == "session"
            assert_event_context(session_event, history_event, 2)

            message_event = websocket.receive_json()
            assert message_event["type"] == "message"
            assert message_event["role"] == "claude"
            assert message_event["content"] == "Saved assistant response"
            assert_event_context(message_event, history_event, 3)

            result_event = websocket.receive_json()
            assert result_event["type"] == "result"
            assert_event_context(result_event, history_event, 4)

        conversations_response = client.get("/api/conversations")
        assert conversations_response.status_code == 200
        conversations = conversations_response.json()
        assert len(conversations) == 1
        assert conversations[0]["title"] == "Remember this request"

        conversation_id = conversations[0]["id"]
        messages_response = client.get(
            f"/api/conversations/{conversation_id}/messages"
        )
        assert messages_response.status_code == 200
        messages = messages_response.json()
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert [message["sequence"] for message in messages] == [1, 2]
        assert messages[0]["content"] == "Remember this request"
        assert messages[1]["content"] == "Saved assistant response"
        assert messages[0]["run_id"] == history_event["run_id"]
        assert messages[1]["run_id"] == history_event["run_id"]

        events_response = client.get(
            f"/api/runs/{history_event['run_id']}/events"
        )
        assert events_response.status_code == 200
        events = events_response.json()
        assert [event["seq"] for event in events] == [1, 2, 3, 4]
        assert [event["type"] for event in events] == [
            "history",
            "session",
            "message",
            "result",
        ]


def test_rest_and_websocket_replay_events(tmp_path, monkeypatch) -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "First block"},
                        {"type": "text", "text": "Second block"},
                    ]
                },
            }
        ),
        *result_lines("First block\nSecond block"),
    ]

    async def fake_start_claude(prompt: str) -> FakeProcess:
        return FakeProcess(lines)

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("Replay this")
            history_event = websocket.receive_json()
            assert websocket.receive_json()["content"] == "First block"
            assert websocket.receive_json()["content"] == "Second block"
            assert websocket.receive_json()["type"] == "result"

        run_id = history_event["run_id"]
        response = client.get(
            f"/api/runs/{run_id}/events",
            params={"after_seq": 1, "limit": 2},
        )
        assert response.status_code == 200
        assert [event["seq"] for event in response.json()] == [2, 3]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "run.subscribe",
                    "run_id": run_id,
                    "after_seq": 2,
                }
            )
            replayed = [
                websocket.receive_json(),
                websocket.receive_json(),
            ]

        assert [event["seq"] for event in replayed] == [3, 4]
        assert [event["type"] for event in replayed] == ["message", "result"]
        assert all(event["run_id"] == run_id for event in replayed)


async def test_websocket_replay_reads_all_pages(tmp_path) -> None:
    database = make_database(tmp_path)
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Large replay")

        async with database.session() as session:
            session.add_all(
                Event(
                    run_id=created.run_id,
                    seq=seq,
                    type="message",
                    payload={
                        "schema_version": 1,
                        "content": f"event-{seq}",
                    },
                )
                for seq in range(1, 502)
            )
            await session.commit()

        websocket = RecordingWebSocket(database)
        await main_module.handle_subscription(
            websocket,
            created.run_id,
            after_seq=0,
        )

        assert len(websocket.messages) == 501
        assert websocket.messages[0]["seq"] == 1
        assert websocket.messages[-1]["seq"] == 501
    finally:
        await database.dispose()


def test_websocket_accepts_multiple_requests(tmp_path, monkeypatch) -> None:
    async def fake_start_claude(prompt: str) -> FakeProcess:
        return FakeProcess(result_lines(f"Response to {prompt}"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("First")
            first_history = websocket.receive_json()
            first_result = websocket.receive_json()
            assert first_history["seq"] == 1
            assert first_result["result"] == "Response to First"
            assert first_result["seq"] == 2

            websocket.send_text("Second")
            second_history = websocket.receive_json()
            second_result = websocket.receive_json()
            assert second_history["seq"] == 1
            assert second_result["result"] == "Response to Second"
            assert second_result["seq"] == 2

        conversations = client.get("/api/conversations").json()
        assert {item["title"] for item in conversations} == {"First", "Second"}


def test_conversation_title_is_truncated(tmp_path, monkeypatch) -> None:
    async def fake_start_claude(prompt: str) -> FakeProcess:
        return FakeProcess(result_lines())

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("x" * 120)
            assert websocket.receive_json()["type"] == "history"
            assert websocket.receive_json()["type"] == "result"

        conversation = client.get("/api/conversations").json()[0]
        assert len(conversation["title"]) == 80
        assert conversation["title"].endswith("…")
