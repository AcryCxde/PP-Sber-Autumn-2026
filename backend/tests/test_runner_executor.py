"""Режим executor=runner: браузерный /ws → транспорт → (поддельный) runner → история backend."""

import hashlib
import sqlite3
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.config import Settings
from backend.db import Database
from backend.tests.fake_docker import FakeDocker, unpack


ANTHROPIC_TOKEN = "test-anthropic-token-0123456789"


def make_runner_app(tmp_path, docker):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'history.db').as_posix()}")
    app = main_module.create_app(database, Settings(
        workspace_root=tmp_path / "workspaces",
        executor="runner",
        anthropic_auth_token=ANTHROPIC_TOKEN,
        runner_transport_db=tmp_path / "data" / "transport.db",
        runner_secret_file=tmp_path / "data" / "gateway.secret",
        runner_supervise_interval_seconds=60,
    ))
    app.state.runner_manager._client = docker
    app.state.runner_manager._environ = {
        "ANTHROPIC_BASE_URL": "https://proxy.example/anthropic",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-5.6-luna",
        "ANTHROPIC_AUTH_TOKEN": ANTHROPIC_TOKEN,
    }
    return app


class FakeRunner(threading.Thread):
    """Читает команды из транспорта и пишет события так, как их прислал бы ctrunner."""

    def __init__(self, transport, docker, script):
        super().__init__(daemon=True)
        self.transport, self.docker, self.script = transport, docker, script
        self.seq = 0
        self.turn: str | None = None
        self.received: list[dict] = []
        self.halt = threading.Event()

    def emit(self, project, kind, payload):
        self.seq += 1
        self.transport.ingest(project, self.seq, {
            "event_id": str(uuid4()), "project_id": project, "run_id": self.turn,
            "occurred_at": "2026-10-25T10:00:00Z", "type": kind, "payload": payload})

    def run(self):
        while not self.halt.is_set():
            with sqlite3.connect(self.transport.database) as db:
                projects = [r[0] for r in db.execute("SELECT DISTINCT project FROM commands WHERE ack IS NULL")]
            for project in projects:
                for command in self.transport.commands(project):
                    self.transport.ack(project, {"id": command["id"], "status": "accepted"})
                    self.received.append(command)
                    if command["kind"] == "message":
                        self.turn = command["id"]
                    container = self.docker.containers.get(f"ct-{project}")
                    for kind, payload in self.script(command, container):
                        self.emit(project, kind, payload)
            time.sleep(0.02)


def run_command(content, conversation_id=None):
    return {"type": "run.create", "conversation_id": conversation_id,
            "client_request_id": str(uuid4()), "content": content}


def receive_until_terminal(websocket):
    events = []
    while True:
        event = websocket.receive_json()
        events.append(event)
        if event["type"] in {"run.completed", "run.failed", "run.outcome_unknown"}:
            return events


def test_run_executes_in_per_conversation_runner_container(tmp_path):
    docker = FakeDocker()
    data = "привет\n".encode()

    def script(command, container):
        container.files["/workspace/project/hello.txt"] = data
        return [
            ("run.started", {}),
            ("message.created", {"role_id": None, "text": "готово"}),
            ("artifact.created", {"artifact_id": "a1", "path": "hello.txt", "commit_sha": "abc123",
                                  "role_id": None, "size_bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()}),
            ("usage.reported", {"input_tokens": 10, "output_tokens": 2,
                                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                                "num_turns": 1, "duration_ms": 1200, "duration_api_ms": 900,
                                "cost_usd": 0.01}),
            ("run.completed", {}),
        ]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("Создай hello.txt"))
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()

        assert [e["type"] for e in events] == [
            "run.accepted", "run.started", "message.created", "artifact.saved", "run.completed"]
        assert events[-1]["status"] == "completed"
        assert events[-1]["session_state"] == "active"
        conversation = events[0]["conversation_id"]
        project = conversation.lower()
        manager = app.state.runner_manager

        # Контейнер на диалог с изоляцией, адресом gateway и без ключей в окружении.
        container = docker.containers.get(f"ct-{project}")
        assert container.kwargs["environment"]["GATEWAY_URL"] == "ws://backend:8000/v1/runner"
        assert container.kwargs["environment"]["PROJECT_ID"] == project
        assert "ANTHROPIC_AUTH_TOKEN" not in container.kwargs["environment"]
        assert container.kwargs["read_only"] is True and container.kwargs["cap_drop"] == ["ALL"]
        assert container.kwargs["volumes"][f"ctsec-{project}"] == {"bind": "/run/secrets", "mode": "ro"}
        [(path, archive)] = docker.containers.helpers[0].archives
        secrets = unpack(archive)
        assert path == "/secrets"
        assert secrets["anthropic_token"][0].decode().strip() == ANTHROPIC_TOKEN
        assert secrets["gateway_token"][0].decode().strip() == manager.token(project)
        assert secrets["gateway_token"][1:] == (0o400, 1000)
        assert docker.containers.helpers[0].removed

        # Артефакт прочитан из контейнера, сохранён и подтверждён транспорту.
        transport = app.state.runner_transport
        run_id = events[0]["run_id"]
        assert transport.status(project, run_id)["status"] == "completed"
        assert "artifact.saved" in [e["type"] for e in transport.events(project, run_id)]
        download = client.get(f"/api/runs/{run_id}/events/{events[3]['seq']}/file")
        assert download.status_code == 200 and download.content == data

        status = client.get(f"/api/conversations/{conversation}/runner").json()
        assert status["state"] == "running" and status["active_run"] is None

        # Удаление диалога убирает и контейнер, и тома сессии.
        assert client.delete(f"/api/conversations/{conversation}").status_code == 204
        assert docker.removed_containers == [f"ct-{project}"]
        assert docker.volumes._items == {}
        assert client.get("/api/conversations").json() == []
        assert client.delete(f"/api/conversations/{conversation}").status_code == 404


def test_conversation_from_cli_mode_requires_reset_before_runner(tmp_path):
    from backend.repository import accept_run, finalize_run

    docker = FakeDocker()
    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        async def cli_conversation():
            async with app.state.database.session() as session:
                accepted = await accept_run(session, conversation_id=None,
                                            client_request_id=str(uuid4()), content="hi")
            async with app.state.database.session() as session:
                await finalize_run(session, accepted.run_id, assistant_content="ok",
                                   status="completed", exit_code=0, session_id=str(uuid4()))
            return accepted.conversation_id

        conversation = client.portal.call(cli_conversation)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(run_command("продолжи", conversation))
            events = receive_until_terminal(websocket)
        assert events[-1]["type"] == "run.failed"
        assert events[-1]["error_code"] == "claude_session_unavailable"
        assert events[-1]["can_reset_context"] is True
        assert docker.started == []


def test_agent_question_is_answered_by_next_message(tmp_path):
    docker = FakeDocker()
    question = {"text": "Какой формат?", "header": None, "multi_select": False, "allow_custom": True,
                "options": [{"label": "md", "description": None}, {"label": "txt", "description": None}]}

    def script(command, container):
        if command["kind"] == "message":
            return [("run.started", {}),
                    ("facilitator.question", {"question_id": "f1", "questions": [question]})]
        return [("user.answer.accepted", {"question_id": "f1", "answers": command["answers"]}),
                ("message.created", {"role_id": None, "text": "Сделал в md"}),
                ("run.completed", {})]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("Сделай отчёт"))
                first = receive_until_terminal(websocket)
                conversation = first[0]["conversation_id"]
                websocket.send_json(run_command("md", conversation))
                second = receive_until_terminal(websocket)
        finally:
            runner.halt.set()

        assert first[-1]["status"] == "completed"
        assert "Какой формат?" in first[-2]["content"]
        assert second[-1]["status"] == "completed"
        assert second[-2]["content"] == "Сделал в md"
        answer = runner.received[-1]
        assert answer["kind"] == "fork_answer" and answer["answers"] == {"Какой формат?": "md"}
        assert answer["id"] == second[0]["run_id"]
        # Один контейнер на диалог: второй запрос не создавал новый.
        assert len(docker.started) == 1


def test_conversation_with_active_run_cannot_be_deleted(tmp_path):
    from backend.repository import accept_run

    docker = FakeDocker()
    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        async def pending_run():
            async with app.state.database.session() as session:
                accepted = await accept_run(session, conversation_id=None,
                                            client_request_id=str(uuid4()), content="hi")
            return accepted.conversation_id

        conversation = client.portal.call(pending_run)
        assert client.delete(f"/api/conversations/{conversation}").status_code == 409
        assert len(client.get("/api/conversations").json()) == 1
