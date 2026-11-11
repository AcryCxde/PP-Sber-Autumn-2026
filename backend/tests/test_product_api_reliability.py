"""Product mutation durability, admission control and concurrent receipt regressions."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import backend.main as main
from backend.models import ProductRun, Project, ProjectFile, ProjectRun, Run
from backend.product import load_workspace
from backend.public_events import question_asked_event
from backend.repository import _append_event_in_transaction, reconcile_orphaned_runs
from backend.tests.auth_helpers import sign_in
from backend.tests.databases import POSTGRES_URL
from backend.tests.test_history_api import make_app
from backend.tests.test_product_api import FakeClaude, plan, post, wait_run


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(main, "start_claude", fake)
    return fake


def test_link_commit_failure_rolls_back_acceptance_and_releases_slot(tmp_path, monkeypatch, claude):
    app = make_app(tmp_path)
    original_commit = AsyncSession.commit

    async def fail_link_commit(session):
        if any(isinstance(item, ProjectRun) for item in session.new):
            raise RuntimeError("link commit unavailable")
        await original_commit(session)

    with TestClient(app, raise_server_exceptions=False) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        monkeypatch.setattr(AsyncSession, "commit", fail_link_commit)
        failed = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand"})
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

        async def counts():
            async with app.state.database.session() as session:
                return [await session.scalar(select(func.count()).select_from(model)) for model in (ProductRun, Run, ProjectRun)]

        assert failed.status_code == 500
        assert client.portal.call(counts) == [0, 0, 0]
        assert app.state.run_task_count == 0
        assert claude.prompts == []
        retried = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand"})
        assert retried.status_code == 201
        assert wait_run(client, project_id, retried.json()["id"])["status"] == "completed"


def test_concurrent_starts_reserve_single_slot_before_acceptance(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    entered = None
    release = None

    async def hold_execution(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(main, "execute_accepted_run", hold_execution)
    with TestClient(app) as client:
        sign_in(client)
        app.state.settings.max_concurrent_runs = 1

        async def gates():
            return asyncio.Event(), asyncio.Event()

        entered, release = client.portal.call(gates)
        bodies = [plan(), plan()]
        ids = [post(client, "projects", body).json()["id"] for body in bodies]
        barrier = Barrier(2)

        def submit(index):
            barrier.wait()
            return post(client, f"projects/{ids[index]}/runs", {**bodies[index], "stageId": "understand"})

        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(submit, range(2)))
        assert sorted(response.status_code for response in responses) == [201, 503]
        assert app.state.run_task_count == 1

        async def count():
            async with app.state.database.session() as session:
                return await session.scalar(select(func.count()).select_from(ProductRun))

        assert client.portal.call(count) == 1
        client.portal.call(release.set)


def test_interrupted_linked_run_becomes_terminal_on_reconciliation(tmp_path, monkeypatch):
    app = make_app(tmp_path)

    async def no_execution(*args):
        pass

    monkeypatch.setattr(main, "execute_accepted_run", no_execution)
    with TestClient(app) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        created = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand"})
        run_id = created.json()["id"]
        assert client.get(f"/api/v2/projects/{project_id}/runs/{run_id}").json()["status"] == "queued"

        async def restart_reconcile():
            async with app.state.database.session() as session:
                await reconcile_orphaned_runs(session)

        client.portal.call(restart_reconcile)
        result = client.get(f"/api/v2/projects/{project_id}/runs/{run_id}").json()
        assert result["status"] == "outcome_unknown"
        async def persisted_error():
            async with app.state.database.session() as session:
                run = await session.scalar(select(Run).join(ProjectRun).where(ProjectRun.product_run_id == run_id))
                project = await session.get(Project, project_id)
                workspace = await load_workspace(session, project)
                return run.error_code, workspace.busy()

        assert client.portal.call(persisted_error) == ("backend_restarted", False)


@pytest.mark.parametrize("change", [{"questionId": "different-question"}, {"answers": {"Budget?": "different"}}, None])
def test_answer_replay_rejects_changed_body(tmp_path, claude, change):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        run_id = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand"}).json()["id"]
        wait_run(client, project_id, run_id)

        async def ask():
            async with app.state.database.session() as session:
                link = await session.scalar(select(ProjectRun).where(ProjectRun.product_run_id == run_id))
                await _append_event_in_transaction(session, link.run_id, allow_terminal=True, event_data=question_asked_event([
                    {"text": "Budget?", "header": "Budget", "options": []},
                ]))
                await session.commit()

        client.portal.call(ask)
        question = client.get(f"/api/v2/projects/{project_id}/runs/{run_id}").json()["question"]
        answer = {"questionId": question["id"], "answers": {"Budget?": "one million"}}
        key = str(uuid4())
        path = f"projects/{project_id}/runs/{run_id}/answers"
        assert post(client, path, answer, key).status_code == 200
        wait_run(client, project_id, run_id, ("completed", "failed"))
        assert post(client, path, answer, key).status_code == 200
        if change is None:
            async def legacy_fingerprint():
                async with app.state.database.session() as session:
                    link = await session.scalar(select(ProjectRun).where(
                        ProjectRun.product_run_id == run_id, ProjectRun.kind == "answer"
                    ))
                    link.request_hash = None
                    await session.commit()

            client.portal.call(legacy_fingerprint)
        conflict = post(client, path, {**answer, **(change or {})}, key)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"
        assert len(claude.prompts) == 2


@pytest.mark.skipif(not POSTGRES_URL, reason="Row-lock concurrency requires PostgreSQL")
@pytest.mark.parametrize("mode", ["same-key", "changed-body", "same-name"])
def test_concurrent_uploads_preserve_receipts_and_unique_names(tmp_path, mode):
    app = make_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        sign_in(client)
        project_id = post(client, "projects", plan()).json()["id"]
        barrier = Barrier(2)

        def upload(index):
            data = b"first" if index == 0 or mode != "changed-body" else b"second"
            key = str(index) if mode == "same-name" else "same-key"
            barrier.wait()
            return client.post(f"/api/v2/projects/{project_id}/files",
                               files={"file": ("notes.txt", data, "text/plain")},
                               data={"clientFileId": key, "sha256": hashlib.sha256(data).hexdigest()})

        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(upload, range(2)))
        assert sorted(response.status_code for response in responses) == ([201, 409] if mode == "changed-body" else [201, 201])
        if mode == "same-key":
            assert responses[0].json() == responses[1].json()

        async def names():
            async with app.state.database.session() as session:
                return list(await session.scalars(select(ProjectFile.name).where(ProjectFile.project_id == project_id)))

        assert set(client.portal.call(names)) == ({"notes.txt", "notes (2).txt"} if mode == "same-name" else {"notes.txt"})
