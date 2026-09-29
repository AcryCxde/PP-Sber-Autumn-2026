import json

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.db import Database


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


def test_history_endpoints_are_empty_for_new_database(tmp_path) -> None:
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/conversations")
        assert response.status_code == 200
        assert response.json() == []

        response = client.get("/api/conversations/missing/messages")
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

            assert websocket.receive_json()["type"] == "session"
            assert websocket.receive_json() == {
                "type": "message",
                "role": "claude",
                "content": "Saved assistant response",
            }
            assert websocket.receive_json()["type"] == "result"

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


def test_websocket_accepts_multiple_requests(tmp_path, monkeypatch) -> None:
    async def fake_start_claude(prompt: str) -> FakeProcess:
        return FakeProcess(result_lines(f"Response to {prompt}"))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = main_module.create_app(make_database(tmp_path))

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("First")
            assert websocket.receive_json()["type"] == "history"
            assert websocket.receive_json()["result"] == "Response to First"

            websocket.send_text("Second")
            assert websocket.receive_json()["type"] == "history"
            assert websocket.receive_json()["result"] == "Response to Second"

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
