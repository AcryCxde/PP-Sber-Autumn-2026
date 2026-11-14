"""AI policy is enforced at public acceptance and execution boundaries."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import main
from backend.config import Settings
from backend.db import Database
from backend.models import Run, User
from backend.repository import RunAcceptanceError, accept_run
from backend.tests.auth_helpers import sign_in
from backend.tests.databases import database_url
from backend.tests.test_history_api import make_app
from backend.tests.test_product_api import FakeClaude, plan, post


async def setup(database):
    await database.create_schema()
    async with database.session() as session:
        user = User(email=f"{uuid4()}@example.test", password_hash="unused")
        session.add(user)
        await session.commit()
        return user.id


def test_frozen_new_acceptance_is_denied_without_mutation(tmp_path):
    async def scenario():
        db = Database(database_url(tmp_path / "ingress.db"))
        owner = await setup(db)
        async with db.session() as session:
            user = await session.get(User, owner)
            user.ai_frozen = True
            await session.commit()
            with pytest.raises(RunAcceptanceError, match="AI") as error:
                await accept_run(session, conversation_id=None, client_request_id=str(uuid4()), content="test", owner_id=owner)
            assert error.value.code == "ai_access_frozen"
            assert list(await session.scalars(select(Run))) == []
        await db.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("frozen", [True, False])
def test_background_wrapper_checks_policy_and_never_executes_twice(tmp_path, monkeypatch, frozen):
    async def scenario():
        db = Database(database_url(tmp_path / "background.db"))
        owner = await setup(db)
        async with db.session() as session:
            accepted = await accept_run(session, conversation_id=None, client_request_id=str(uuid4()), content="test", owner_id=owner)
            user = await session.get(User, owner)
            user.ai_frozen = frozen
            await session.commit()
        calls = []
        async def adapter(*args, **kwargs):
            calls.append(kwargs["admission"].run_id)
        monkeypatch.setattr(main, "run_via_runner", adapter)
        state = SimpleNamespace(database=db, settings=Settings(executor="runner"))
        events = []
        async def deliver(event):
            events.append(event.type)
        await main.execute_accepted_run(state, accepted, "test", deliver)
        await main.execute_accepted_run(state, accepted, "test", deliver)
        assert calls == ([] if frozen else [accepted.run_id])
        assert events.count("run.started") == (0 if frozen else 1)
        async with db.session() as session:
            run = await session.get(Run, accepted.run_id)
            assert run.status == ("failed" if frozen else "running")
            assert (run.started_at is None) == frozen
        await db.dispose()
    asyncio.run(scenario())


def test_replay_of_accepted_run_survives_freeze(tmp_path):
    async def scenario():
        db = Database(database_url(tmp_path / "replay.db"))
        owner = await setup(db)
        key = str(uuid4())
        async with db.session() as session:
            first = await accept_run(session, conversation_id=None, client_request_id=key, content="test", owner_id=owner)
            user = await session.get(User, owner)
            user.ai_frozen = True
            await session.commit()
            replay = await accept_run(session, conversation_id=None, client_request_id=key, content="test", owner_id=owner)
            assert replay.idempotent_replay and replay.run_id == first.run_id
        await db.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("wire", ["raw", "resume", "reset"])
def test_frozen_websocket_never_calls_cli(tmp_path, monkeypatch, wire):
    fake = FakeClaude()
    monkeypatch.setattr(main, "start_claude", fake)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        async def freeze():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.ai_frozen = True
                await session.commit()
        client.portal.call(freeze)
        with client.websocket_connect("/ws") as ws:
            if wire == "raw":
                ws.send_text("hello")
            else:
                ws.send_json({"type": "run.create", "client_request_id": str(uuid4()), "content": "hello", "context_mode": wire})
            event = ws.receive_json()
            assert event["type"] == "command.error"
            assert event["code"] == "ai_access_frozen"
        assert fake.prompts == []
        assert client.get("/api/auth/me").status_code == 200


def test_frozen_v2_start_returns_403_without_cli(tmp_path, monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(main, "start_claude", fake)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        body = plan()
        project = post(client, "projects", body).json()
        async def freeze():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.ai_frozen = True
                await session.commit()
        client.portal.call(freeze)
        response = post(client, f"projects/{project['id']}/runs", {**body, "stageId": "understand", "fileIds": []})
        assert response.status_code == 403
        assert response.json()["code"] == "ai_access_frozen"
        assert fake.prompts == []


def test_admitted_work_continues_after_freeze_but_new_work_does_not(tmp_path, monkeypatch):
    async def scenario():
        from backend.repository import finalize_run
        db = Database(database_url(tmp_path / "continue.db"))
        owner = await setup(db)
        async with db.session() as session:
            accepted = await accept_run(session, conversation_id=None, client_request_id=str(uuid4()), content="test", owner_id=owner)
        calls = []
        async def adapter(state, acceptance, content, deliver, *, admission):
            calls.append("first internal call")
            async with db.session() as session:
                user = await session.get(User, owner)
                user.ai_frozen = True
                await session.commit()
            calls.append("second internal call")
            async with db.session() as session:
                await finalize_run(session, acceptance.run_id, assistant_content="done",
                                   status="completed", exit_code=0, session_id="saved")
        monkeypatch.setattr(main, "run_via_runner", adapter)
        async def deliver(event):
            pass
        state = SimpleNamespace(database=db, settings=Settings(executor="runner"))
        await main.execute_accepted_run(state, accepted, "test", deliver)
        assert len(calls) == 2
        async with db.session() as session:
            run = await session.get(Run, accepted.run_id)
            assert run.status == "completed"
            with pytest.raises(RunAcceptanceError) as error:
                await accept_run(session, conversation_id=accepted.conversation_id, client_request_id=str(uuid4()),
                                 content="answer", requested_context_mode="resume", owner_id=owner)
            assert error.value.code == "ai_access_frozen"
            user = await session.get(User, owner)
            user.ai_frozen = False
            await session.commit()
            new = await accept_run(session, conversation_id=accepted.conversation_id, client_request_id=str(uuid4()),
                                   content="answer", owner_id=owner)
            assert new.run_id != accepted.run_id
        await db.dispose()
    asyncio.run(scenario())


def test_unknown_owner_fails_closed_and_finalizes_pending(tmp_path, monkeypatch):
    async def scenario():
        db = Database(database_url(tmp_path / "owner.db"))
        await db.create_schema()
        async with db.session() as session:
            accepted = await accept_run(session, conversation_id=None, client_request_id=str(uuid4()), content="legacy")
        async def forbidden(*args, **kwargs):
            raise AssertionError("executor must not run")
        monkeypatch.setattr(main, "run_via_runner", forbidden)
        async def deliver(event):
            pass
        await main.execute_accepted_run(SimpleNamespace(database=db, settings=Settings(executor="runner")),
                                        accepted, "legacy", deliver)
        async with db.session() as session:
            run = await session.get(Run, accepted.run_id)
            assert run.status == "failed"
            assert run.started_at is None
            assert run.error_code == "ai_policy_unavailable"
        await db.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["stage", "revision", "answer"])
def test_frozen_legacy_project_is_denied_before_preparation(tmp_path, monkeypatch, kind):
    fake = FakeClaude()
    monkeypatch.setattr(main, "start_claude", fake)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        project = client.post("/api/projects", json={"example": True}).json()
        async def freeze():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.ai_frozen = True
                await session.commit()
        client.portal.call(freeze)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "project.run", "project_id": project["id"],
                          "kind": kind, "text": "test", "client_request_id": str(uuid4())})
            assert ws.receive_json()["code"] == "ai_access_frozen"
        assert fake.prompts == []


def test_frozen_v2_answer_is_denied_with_existing_session(tmp_path, monkeypatch):
    from backend.models import ProjectRun
    from backend.repository import _append_event_in_transaction
    from backend.public_events import question_asked_event
    from backend.tests.test_product_api import wait_run
    fake = FakeClaude()
    monkeypatch.setattr(main, "start_claude", fake)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        body = plan()
        project = post(client, "projects", body).json()
        run = post(client, f"projects/{project['id']}/runs", {**body, "stageId": "understand"}).json()
        wait_run(client, project["id"], run["id"])
        async def freeze_and_ask():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.ai_frozen = True
                link = await session.scalar(select(ProjectRun).where(ProjectRun.product_run_id == run["id"]))
                await _append_event_in_transaction(session, link.run_id, allow_terminal=True,
                    event_data=question_asked_event([{"text": "Budget?", "header": "Budget", "options": []}]))
                await session.commit()
        client.portal.call(freeze_and_ask)
        question = client.get(f"/api/v2/projects/{project['id']}/runs/{run['id']}").json()["question"]
        before = len(fake.prompts)
        response = post(client, f"projects/{project['id']}/runs/{run['id']}/answers",
                        {"questionId": question["id"], "answers": {"Budget?": "yes"}})
        assert response.status_code == 403
        assert response.json()["code"] == "ai_access_frozen"
        assert len(fake.prompts) == before


def test_frozen_v2_rework_is_denied_before_source_read(tmp_path, monkeypatch):
    from backend.tests.test_product_api import started_project
    fake = FakeClaude()
    monkeypatch.setattr(main, "start_claude", fake)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        project_id, body, completed = started_project(client, fake)
        async def freeze():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.ai_frozen = True
                await session.commit()
        client.portal.call(freeze)
        before = len(fake.prompts)
        response = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand",
            "parentRunId": completed["id"], "text": "revise"})
        assert response.status_code == 403
        assert response.json()["code"] == "ai_access_frozen"
        assert len(fake.prompts) == before


@pytest.mark.parametrize("wire", ["raw", "run.create", "project.run"])
def test_frozen_runner_public_ingress_never_dispatches(tmp_path, wire):
    from backend.tests.fake_docker import FakeDocker
    from backend.tests.test_runner_executor import make_runner_app
    app = make_runner_app(tmp_path, FakeDocker())
    with TestClient(app) as client:
        sign_in(client)
        project = client.post("/api/projects", json={"example": True}).json()
        async def freeze():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                user.ai_frozen = True
                await session.commit()
        client.portal.call(freeze)
        with client.websocket_connect("/ws") as ws:
            if wire == "raw":
                ws.send_text("test")
            else:
                command = {"type": wire, "client_request_id": str(uuid4()), "content": "test"}
                if wire == "project.run":
                    command.update(project_id=project["id"], kind="stage")
                ws.send_json(command)
            assert ws.receive_json()["code"] == "ai_access_frozen"
        assert app.state.runner_transport.commands(project["conversation_id"]) == []
        assert app.state.runner_manager._client.started == []
