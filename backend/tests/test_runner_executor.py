"""Режим executor=runner: браузерный /ws → транспорт → (поддельный) runner → история backend."""

import asyncio
import hashlib
import sqlite3
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import backend.main as main_module
from backend.artifact_confirmations import ReconciliationResult
from backend.config import Settings
from backend.db import Database
from backend.models import Artifact, ArtifactConfirmation, Conversation, RunnerContainer, Run, User
from backend.tests.databases import database_url
from backend.tests.auth_helpers import sign_in
from backend.tests.fake_docker import FakeDocker, unpack


ANTHROPIC_TOKEN = "test-anthropic-token-0123456789"


def make_runner_app(tmp_path, docker):
    database = Database(database_url(tmp_path / "data" / "history.db"))
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
        sign_in(client)
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

        # Состояние чужого runner недоступно: ответ как для несуществующего.
        cookies = dict(client.cookies)
        client.cookies.clear()
        sign_in(client, "beta@example.com")
        assert client.get(f"/api/conversations/{conversation}/runner").status_code == 404
        client.cookies.clear()
        client.cookies.update(cookies)

        # Удаление диалога убирает и контейнер, и тома сессии.
        assert client.delete(f"/api/conversations/{conversation}").status_code == 204
        assert docker.removed_containers == [f"ct-{project}"]
        assert docker.volumes._items == {}
        assert client.get("/api/conversations").json() == []
        assert client.delete(f"/api/conversations/{conversation}").status_code == 404


def test_runner_artifact_without_optional_facts_uses_verified_bytes(tmp_path):
    docker = FakeDocker()
    data = b"verified bytes\n"

    def script(command, container):
        container.files["/workspace/project/result.txt"] = data
        return [
            ("run.started", {}),
            ("artifact.created", {"artifact_id": "a1", "path": "result.txt",
                                  "commit_sha": "abc123", "role_id": None}),
            ("run.completed", {}),
        ]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("Сохрани файл"))
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()

        assert events[-1]["type"] == "run.completed"
        async def persisted_facts():
            async with app.state.database.session() as session:
                confirmation = await session.scalar(select(ArtifactConfirmation))
                assert confirmation is not None
                return confirmation.size_bytes, confirmation.sha256
        assert client.portal.call(persisted_facts) == (len(data), hashlib.sha256(data).hexdigest())


def test_runner_artifact_is_committed_before_transport_confirmation(tmp_path, monkeypatch):
    docker = FakeDocker()
    data = "строгий UTF-8\r\n".encode()
    confirmations: list[dict] = []

    def script(command, container):
        container.files["/workspace/project/result.txt"] = data
        return [
            ("run.started", {}),
            ("artifact.created", {"artifact_id": "a1", "path": "result.txt", "commit_sha": "abc123",
                                  "role_id": None, "size_bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()}),
            ("run.completed", {}),
        ]

    app = make_runner_app(tmp_path, docker)
    original_confirm = app.state.runner_transport.confirm_saved

    def confirm_after_commit(project, run_id, artifact_id, receipt):
        confirmations.append(receipt)
        original_confirm(project, run_id, artifact_id, receipt)

    monkeypatch.setattr(app.state.runner_transport, "confirm_saved", confirm_after_commit)
    with TestClient(app) as client:
        sign_in(client)
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("Сохрани файл"))
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()

        assert [event["type"] for event in events] == [
            "run.accepted", "run.started", "artifact.saved", "run.completed",
        ]
        assert confirmations == [{
            "artifact_id": "a1",
            "version_id": "abc123",
            "size_bytes": len(data),
            "download": f"/api/runs/{events[0]['run_id']}/events/{events[2]['seq']}/file",
        }]

        async def persisted_counts():
            async with app.state.database.session() as session:
                return (
                    await session.scalar(select(func.count()).select_from(Artifact)),
                    await session.scalar(select(func.count()).select_from(ArtifactConfirmation)),
                )

        assert client.portal.call(persisted_counts) == (1, 1)
        assert [event["type"] for event in app.state.runner_transport.events(
            events[0]["conversation_id"].lower(), events[0]["run_id"],
        )][-2:] == ["artifact.saved", "run.completed"]


def test_immediate_save_ignores_unrelated_confirmation_conflict(tmp_path, monkeypatch):
    docker = FakeDocker()
    data = b"current artifact"

    def script(command, container):
        container.files["/workspace/project/current.txt"] = data
        return [
            ("run.started", {}),
            ("artifact.created", {"artifact_id": "current", "path": "current.txt", "commit_sha": "v1",
                                  "role_id": None, "size_bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()}),
            ("run.completed", {}),
        ]

    app = make_runner_app(tmp_path, docker)
    async def reconcile_with_unrelated_conflict(database, transport, settings):
        return ReconciliationResult(
            claimed=1,
            confirmed=0,
            conflicted=1,
            rescheduled=0,
            conflicted_confirmation_ids=frozenset({"unrelated-confirmation"}),
            terminal_events=(),
        )

    monkeypatch.setattr(
        "backend.runner_executor.reconcile_artifact_confirmations",
        reconcile_with_unrelated_conflict,
    )
    with TestClient(app) as client:
        sign_in(client)
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("Сохрани файл"))
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()

    assert events[-1]["type"] == "run.completed"


def test_runner_startup_reconciles_before_orphaning_awaiting_storage(tmp_path, monkeypatch):
    docker = FakeDocker()
    database = Database(database_url(tmp_path / "data" / "history.db"))
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        executor="runner",
        anthropic_auth_token=ANTHROPIC_TOKEN,
        runner_transport_db=tmp_path / "data" / "transport.db",
        runner_secret_file=tmp_path / "data" / "gateway.secret",
        runner_supervise_interval_seconds=60,
        artifact_confirmation_reconcile_interval_seconds=60,
    )

    async def seed() -> tuple[str, str, str]:
        await database.migrate()
        async with database.session() as session:
            user = User(email="restart@example.com", password_hash="x")
            conversation = Conversation(title="Restart")
            session.add_all([user, conversation])
            await session.flush()
            run = Run(conversation_id=conversation.id, client_request_id="restart-request", status="running")
            session.add(run)
            await session.flush()
            project_id = conversation.id.lower()
            confirmation = ArtifactConfirmation(
                run_id=run.id,
                project_id=project_id,
                artifact_id="artifact-1",
                source_event_seq=1,
                version_id="v1",
                receipt={"artifact_id": "artifact-1", "version_id": "v1", "size_bytes": 1,
                         "download": "/artifact"},
                sha256="a" * 64,
                size_bytes=1,
            )
            session.add(confirmation)
            await session.commit()
            return run.id, project_id, conversation.id

    run_id, project_id, _ = asyncio.run(seed())
    transport = main_module.RunnerTransport(settings.runner_transport_db)
    transport.message(project_id, run_id, "restart-request", "start")
    transport.ingest(project_id, 1, {
        "event_id": str(uuid4()), "project_id": project_id, "run_id": run_id,
        "occurred_at": "2026-10-30T00:00:00Z", "type": "artifact.created",
        "payload": {"artifact_id": "artifact-1", "path": "result.txt", "commit_sha": "v1",
                    "role_id": None, "size_bytes": 1, "sha256": "a" * 64},
    })
    transport.ingest(project_id, 2, {
        "event_id": str(uuid4()), "project_id": project_id, "run_id": run_id,
        "occurred_at": "2026-10-30T00:00:01Z", "type": "run.completed", "payload": {},
    })
    assert transport.status(project_id, run_id)["awaiting_storage"]

    app = main_module.create_app(database, settings)
    app.state.runner_manager._client = docker
    app.state.runner_manager._environ = {
        "ANTHROPIC_BASE_URL": "https://proxy.example/anthropic",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-5.6-luna",
        "ANTHROPIC_AUTH_TOKEN": ANTHROPIC_TOKEN,
    }
    calls = 0
    original_confirm = app.state.runner_transport.confirm_saved

    def confirm_once(project, confirmed_run_id, artifact_id, receipt):
        nonlocal calls
        calls += 1
        original_confirm(project, confirmed_run_id, artifact_id, receipt)

    monkeypatch.setattr(app.state.runner_transport, "confirm_saved", confirm_once)
    with TestClient(app):
        async def states():
            async with database.session() as session:
                run = await session.get(Run, run_id)
                confirmation = await session.scalar(select(ArtifactConfirmation))
                return run.status, confirmation.status

        assert asyncio.run(states()) == ("outcome_unknown", "confirmed")
        assert app.state.runner_transport.status(project_id, run_id)["status"] == "completed"
        assert calls == 1

    async def dispose() -> None:
        await database.dispose()

    asyncio.run(dispose())


def test_runner_startup_conflict_never_false_completes(tmp_path, monkeypatch):
    docker = FakeDocker()
    database = Database(database_url(tmp_path / "data" / "history.db"))
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        executor="runner",
        anthropic_auth_token=ANTHROPIC_TOKEN,
        runner_transport_db=tmp_path / "data" / "transport.db",
        runner_secret_file=tmp_path / "data" / "gateway.secret",
        runner_supervise_interval_seconds=60,
    )

    async def seed() -> tuple[str, str]:
        await database.migrate()
        async with database.session() as session:
            user = User(email="conflict@example.com", password_hash="x")
            conversation = Conversation(title="Conflict")
            session.add_all([user, conversation])
            await session.flush()
            run = Run(conversation_id=conversation.id, client_request_id="conflict-request", status="running")
            session.add(run)
            await session.flush()
            project_id = conversation.id.lower()
            session.add(ArtifactConfirmation(
                run_id=run.id, project_id=project_id, artifact_id="artifact-1", source_event_seq=1,
                version_id="v1", receipt={"artifact_id": "artifact-1", "version_id": "v1",
                                             "size_bytes": 1, "download": "/artifact"},
                sha256="a" * 64, size_bytes=1,
            ))
            await session.commit()
            return run.id, project_id

    run_id, project_id = asyncio.run(seed())
    transport = main_module.RunnerTransport(settings.runner_transport_db)
    transport.message(project_id, run_id, "conflict-request", "start")
    transport.ingest(project_id, 1, {
        "event_id": str(uuid4()), "project_id": project_id, "run_id": run_id,
        "occurred_at": "2026-10-30T00:00:00Z", "type": "artifact.created",
        "payload": {"artifact_id": "artifact-1", "path": "result.txt", "commit_sha": "v1",
                    "role_id": None, "size_bytes": 1, "sha256": "a" * 64},
    })
    transport.ingest(project_id, 2, {
        "event_id": str(uuid4()), "project_id": project_id, "run_id": run_id,
        "occurred_at": "2026-10-30T00:00:01Z", "type": "run.completed", "payload": {},
    })
    transport.confirm_saved(project_id, run_id, "artifact-1", {
        "artifact_id": "artifact-1", "version_id": "other", "size_bytes": 1, "download": "/other",
    })

    app = main_module.create_app(database, settings)
    app.state.runner_manager._client = docker
    app.state.runner_manager._environ = {
        "ANTHROPIC_BASE_URL": "https://proxy.example/anthropic",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-5.6-luna",
        "ANTHROPIC_AUTH_TOKEN": ANTHROPIC_TOKEN,
    }
    with TestClient(app):
        async def states():
            async with database.session() as session:
                run = await session.get(Run, run_id)
                confirmation = await session.scalar(select(ArtifactConfirmation))
                return run.status, confirmation.status

        assert asyncio.run(states()) == ("outcome_unknown", "conflicted")
        assert app.state.runner_transport.status(project_id, run_id)["status"] == "completed"

    asyncio.run(database.dispose())


def test_runner_artifact_rejection_has_no_persisted_receipt(tmp_path):
    docker = FakeDocker()
    data = b"\xffnot-utf8"

    def script(command, container):
        container.files["/workspace/project/result.txt"] = data
        return [
            ("run.started", {}),
            ("artifact.created", {"artifact_id": "a1", "path": "result.txt", "commit_sha": "abc123",
                                  "role_id": None, "size_bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()}),
        ]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("Сохрани файл"))
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()

        assert events[-1]["type"] == "run.failed"
        assert events[-1]["error_code"] == "encoding"

        async def persisted_counts():
            async with app.state.database.session() as session:
                return (
                    await session.scalar(select(func.count()).select_from(Artifact)),
                    await session.scalar(select(func.count()).select_from(ArtifactConfirmation)),
                )

        assert client.portal.call(persisted_counts) == (0, 0)
        transport_events = app.state.runner_transport.events(
            events[0]["conversation_id"].lower(), events[0]["run_id"],
        )
        assert "artifact.saved" not in [event["type"] for event in transport_events]


def test_conversation_from_cli_mode_requires_reset_before_runner(tmp_path):
    from backend.repository import accept_run, finalize_run

    docker = FakeDocker()
    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        owner_id = sign_in(client)["id"]

        async def cli_conversation():
            async with app.state.database.session() as session:
                accepted = await accept_run(session, conversation_id=None,
                                            client_request_id=str(uuid4()), content="hi",
                                            owner_id=owner_id)
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
        sign_in(client)
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
        assert "Какой формат?" in first[-3]["content"]
        # Тот же вопрос структурно: интерфейс проекта показывает варианты кнопками.
        asked = first[-2]
        assert asked["type"] == "question.asked"
        assert asked["questions"] == [{
            "text": "Какой формат?", "header": None, "multi_select": False,
            "options": [{"label": "md", "description": None}, {"label": "txt", "description": None}],
        }]
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
        owner_id = sign_in(client)["id"]

        async def pending_run():
            async with app.state.database.session() as session:
                accepted = await accept_run(session, conversation_id=None,
                                            client_request_id=str(uuid4()), content="hi",
                                            owner_id=owner_id)
            return accepted.conversation_id

        conversation = client.portal.call(pending_run)
        assert client.delete(f"/api/conversations/{conversation}").status_code == 409
        assert len(client.get("/api/conversations").json()) == 1


def test_project_materials_reach_runner_container(tmp_path):
    docker = FakeDocker()

    def script(command, container):
        return [("run.started", {}),
                ("message.created", {"role_id": None, "text": "# Карта задачи\nготово"}),
                ("run.completed", {})]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        project = client.post("/api/projects", json={"example": True}).json()
        client.post(f"/api/projects/{project['id']}/files",
                    files={"file": ("план.md", "# План".encode(), "text/markdown")})
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json({"type": "project.run", "project_id": project["id"],
                                     "client_request_id": str(uuid4()), "kind": "stage"})
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()
        state = client.get(f"/api/projects/{project['id']}").json()

    assert events[-1]["status"] == "completed"
    container = docker.containers.get(f"ct-{project['conversation_id'].lower()}")
    [(path, archive)] = container.archives
    files = unpack(archive)
    assert path == "/workspace/project"
    assert files["materials/план.md"][0] == "# План".encode()
    assert files["materials/план.md"][2] == 1000
    # Промпт этапа уходит runner сообщением и упоминает материалы.
    assert "materials/" in runner.received[0]["text"]
    assert state["phase"] == "review"
    assert state["results"][0][0]["content"].startswith("# Карта задачи")


def test_chat_documents_reach_runner_container(tmp_path):
    docker = FakeDocker()

    def script(command, container):
        return [("run.started", {}),
                ("message.created", {"role_id": None, "text": "Готово"}),
                ("run.completed", {})]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        conversation_id = client.post("/api/conversations").json()["id"]
        client.post(f"/api/conversations/{conversation_id}/files",
                    files={"file": ("бриф.md", "# Бриф".encode(), "text/markdown")})
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json({"type": "run.create", "conversation_id": conversation_id,
                                     "client_request_id": str(uuid4()), "content": "Сделай план"})
                events = receive_until_terminal(websocket)
        finally:
            runner.halt.set()

    assert events[-1]["status"] == "completed"
    container = docker.containers.get(f"ct-{conversation_id.lower()}")
    [(_, archive)] = container.archives
    assert unpack(archive)["materials/бриф.md"][0] == "# Бриф".encode()
    text = runner.received[0]["text"]
    assert text.startswith("Сделай план") and "materials/" in text and "бриф.md" in text


def test_agent_question_pauses_project_until_answer(tmp_path):
    docker = FakeDocker()
    question = {"text": "С какого подхода начнём?", "header": None, "multi_select": False,
                "allow_custom": True,
                "options": [{"label": "Небольшая проверка", "description": None}]}

    def script(command, container):
        if command["kind"] == "message":
            return [("run.started", {}),
                    ("facilitator.question", {"question_id": "f1", "questions": [question]})]
        return [("user.answer.accepted", {"question_id": "f1", "answers": command["answers"]}),
                ("message.created", {"role_id": None, "text": "# Карта задачи\nс проверкой"}),
                ("run.completed", {})]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        project = client.post("/api/projects", json={"example": True}).json()
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json({"type": "project.run", "project_id": project["id"],
                                     "client_request_id": str(uuid4()), "kind": "stage"})
                receive_until_terminal(websocket)
                waiting = client.get(f"/api/projects/{project['id']}").json()
                websocket.send_json({"type": "project.run", "project_id": project["id"],
                                     "client_request_id": str(uuid4()), "kind": "answer",
                                     "text": "Небольшая проверка"})
                receive_until_terminal(websocket)
        finally:
            runner.halt.set()
        state = client.get(f"/api/projects/{project['id']}").json()

    assert waiting["phase"] == "waiting"
    assert waiting["question"][0]["options"] == [{"label": "Небольшая проверка", "description": None}]
    # Вопрос — не версия результата.
    assert waiting["results"][0] == []
    assert runner.received[-1]["answers"] == {"С какого подхода начнём?": "Небольшая проверка"}
    assert state["phase"] == "review"
    assert [item["content"] for item in state["results"][0]] == ["# Карта задачи\nс проверкой"]


def test_deleting_project_removes_its_runner_container_and_volumes(tmp_path):
    docker = FakeDocker()

    def script(command, container):
        return [("run.started", {}), ("message.created", {"role_id": None, "text": "# Итог"}),
                ("run.completed", {})]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        project = client.post("/api/projects", json={"example": True}).json()
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json({"type": "project.run", "project_id": project["id"],
                                     "client_request_id": str(uuid4()), "kind": "stage"})
                receive_until_terminal(websocket)
        finally:
            runner.halt.set()
        records = container_records(client, project["conversation_id"])
        assert [(item.status, item.last_run_id is not None) for item in records] == [("running", True)]
        assert records[0].docker_container_id == docker.containers._items[records[0].container_name].id
        assert records[0].image_tag == "coreteams-runner:dev"
        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        records = container_records(client, project["conversation_id"])
        assert [(item.status, item.stop_reason) for item in records] == [("discarded", "project_deleted")]

    assert docker.removed_containers == [f"ct-{project['conversation_id'].lower()}"]
    assert docker.volumes._items == {}


def container_records(client, conversation_id):
    async def load():
        async with client.app.state.database.session() as session:
            return list(await session.scalars(
                select(RunnerContainer).where(RunnerContainer.conversation_id == conversation_id)
            ))

    return client.portal.call(load)


def test_restore_closes_records_of_containers_missing_in_docker(tmp_path):
    docker = FakeDocker()

    def script(command, container):
        return [("run.started", {}), ("message.created", {"role_id": None, "text": "готово"}),
                ("run.completed", {})]

    app = make_runner_app(tmp_path, docker)
    with TestClient(app) as client:
        sign_in(client)
        runner = FakeRunner(app.state.runner_transport, docker, script)
        runner.start()
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(run_command("привет"))
                conversation_id = receive_until_terminal(websocket)[-1]["conversation_id"]
        finally:
            runner.halt.set()
        # Контейнер удалили в обход backend, например docker rm вручную.
        docker.containers._items.clear()
        client.portal.call(app.state.runner_manager.restore)
        records = container_records(client, conversation_id)
        assert [(item.status, item.stop_reason) for item in records] == [("gone", "missing_on_restore")]
