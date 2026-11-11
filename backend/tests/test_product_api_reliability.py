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


def test_deselected_files_are_not_sent_to_initial_prompt_or_workspace(tmp_path, claude):
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        data = b"deselected secret"
        uploaded = client.post(f"/api/v2/projects/{project_id}/files", files={"file": ("secret.txt", data)},
                               data={"clientFileId": "secret", "sha256": hashlib.sha256(data).hexdigest()})
        assert uploaded.status_code == 201
        created = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand", "fileIds": []})
        wait_run(client, project_id, created.json()["id"])
        assert "secret.txt" not in claude.prompts[0]
        assert claude.materials[0] == {}


def test_approval_replay_checks_body_artifact_identity(tmp_path, claude):
    from backend.tests.test_product_api import started_project
    with TestClient(make_app(tmp_path)) as client:
        sign_in(client)
        project_id, _, run = started_project(client, claude)
        artifact = run["artifacts"][0]
        path = f"projects/{project_id}/runs/{run['id']}/artifacts/{artifact['id']}/approve"
        key = str(uuid4())
        assert post(client, path, {"artifactId": artifact["id"], "version": 1}, key).status_code == 200
        replay = post(client, path, {"artifactId": "different", "version": 1}, key)
        assert replay.status_code == 409
        assert replay.json()["code"] == "idempotency_conflict"


def test_rework_does_not_silently_replace_unavailable_full_source_with_empty_text(tmp_path, claude):
    from backend.models import Artifact
    from backend.tests.test_product_api import started_project
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        project_id, body, run = started_project(client, claude)
        source = run["artifacts"][0]

        async def remove_inline_source():
            async with app.state.database.session() as session:
                artifact = await session.get(Artifact, int(source["id"][1:]))
                artifact.content = None
                artifact.storage_key = "missing-full-source"
                await session.commit()

        client.portal.call(remove_inline_source)
        response = post(client, f"projects/{project_id}/runs", {
            **body, "stageId": source["stageId"], "parentRunId": run["id"],
            "sourceArtifactId": source["id"], "documentId": source["documentId"],
            "sourceVersion": source["version"], "text": "Add details", "fileIds": [],
        })
        assert response.status_code == 503
        assert len(claude.prompts) == 1


@pytest.mark.skipif(not POSTGRES_URL, reason="Root lock requires PostgreSQL")
def test_initial_validation_holds_project_root_lock(tmp_path, monkeypatch, claude):
    from sqlalchemy.exc import DBAPIError
    import backend.api.product as api
    original = api.prepare_initial
    app = make_app(tmp_path)

    async def validate(session, project, workspace, run):
        async with app.state.database.session() as other:
            locked = False
            try:
                await other.execute(select(Project.id).where(Project.id == project.id).with_for_update(nowait=True))
            except DBAPIError:
                locked = True
            assert locked, "Project root was not locked before validation"
        return await original(session, project, workspace, run)

    monkeypatch.setattr(api, "prepare_initial", validate)
    with TestClient(app) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        result = post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand"})
        assert result.status_code == 201
        wait_run(client, project_id, result.json()["id"])


@pytest.mark.skipif(not POSTGRES_URL, reason="Root lock requires PostgreSQL")
def test_approval_checks_receipts_under_project_root_lock(tmp_path, monkeypatch, claude):
    from sqlalchemy.exc import DBAPIError
    import backend.api.product as api
    from backend.tests.test_product_api import started_project
    app = make_app(tmp_path)

    original_chain = api.require_chain

    async def chain(session, project, run_id):
        async with app.state.database.session() as other:
            locked = False
            try:
                await other.execute(select(Project.id).where(Project.id == project.id).with_for_update(nowait=True))
            except DBAPIError:
                locked = True
            assert locked, "Project root was not locked before validation"
        return await original_chain(session, project, run_id)

    with TestClient(app) as client:
        sign_in(client)
        project_id, _, run = started_project(client, claude)
        monkeypatch.setattr(api, "require_chain", chain)
        artifact = run["artifacts"][0]
        path = f"projects/{project_id}/runs/{run['id']}/artifacts/{artifact['id']}/approve"
        assert post(client, path, {"artifactId": artifact["id"], "version": 1}).status_code == 200


@pytest.mark.parametrize("text", ["", "Cafe\u0301 — Привет", "# Large\n" + "details\n" * 20000])
def test_rework_reads_exact_verified_full_storage_source(tmp_path, claude, text):
    from backend.models import Artifact
    from backend.storage import MemoryStorage
    from backend.tests.test_product_api import started_project
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        project_id, body, run = started_project(client, claude)
        source = run["artifacts"][0]
        storage = MemoryStorage()
        app.state.storage = storage
        data = text.encode("utf-8")

        async def replace_source():
            await storage.put("full-source", data, "text/markdown")
            async with app.state.database.session() as session:
                artifact = await session.get(Artifact, int(source["id"][1:]))
                artifact.content = None
                artifact.storage_key = "full-source"
                artifact.sha256 = hashlib.sha256(data).hexdigest()
                artifact.size_bytes = len(data)
                await session.commit()

        client.portal.call(replace_source)
        result = post(client, f"projects/{project_id}/runs", {
            **body, "stageId": source["stageId"], "parentRunId": run["id"],
            "sourceArtifactId": source["id"], "documentId": source["documentId"],
            "sourceVersion": source["version"], "text": "Add details", "fileIds": [],
        })
        assert result.status_code == 201, result.text
        wait_run(client, project_id, result.json()["id"])
        assert text in claude.prompts[1]


@pytest.mark.parametrize("problem", ["missing-hash", "wrong-hash", "nul", "invalid-utf8"])
def test_rework_rejects_unverifiable_source_without_execution(tmp_path, claude, problem):
    from backend.models import Artifact
    from backend.storage import MemoryStorage
    from backend.tests.test_product_api import started_project
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        project_id, body, run = started_project(client, claude)
        source = run["artifacts"][0]
        storage = MemoryStorage()
        app.state.storage = storage
        data = b"a\x00b" if problem == "nul" else b"\xff" if problem == "invalid-utf8" else b"source"

        async def replace_source():
            await storage.put("full-source", data, "text/markdown")
            async with app.state.database.session() as session:
                artifact = await session.get(Artifact, int(source["id"][1:]))
                artifact.content = None
                artifact.storage_key = "full-source"
                artifact.sha256 = None if problem == "missing-hash" else "0" * 64 if problem == "wrong-hash" else hashlib.sha256(data).hexdigest()
                artifact.size_bytes = len(data)
                await session.commit()

        client.portal.call(replace_source)
        result = post(client, f"projects/{project_id}/runs", {
            **body, "stageId": source["stageId"], "parentRunId": run["id"],
            "sourceArtifactId": source["id"], "documentId": source["documentId"],
            "sourceVersion": source["version"], "text": "Add details", "fileIds": [],
        })
        assert result.status_code == (409 if problem == "invalid-utf8" else 503)
        assert len(claude.prompts) == 1


@pytest.mark.skipif(not POSTGRES_URL, reason="Concurrent mutations require PostgreSQL")
@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_approval_returns_one_receipt(tmp_path, claude, same_key):
    from backend.tests.test_product_api import started_project
    with TestClient(make_app(tmp_path), raise_server_exceptions=False) as client:
        sign_in(client)
        project_id, _, run = started_project(client, claude)
        artifact = run["artifacts"][0]
        path = f"projects/{project_id}/runs/{run['id']}/artifacts/{artifact['id']}/approve"
        barrier = Barrier(2)
        key = str(uuid4())

        def approve(index):
            barrier.wait()
            return post(client, path, {"artifactId": artifact["id"], "version": 1}, key if same_key else str(uuid4()))

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(approve, range(2)))
        assert [response.status_code for response in results] == [200, 200]
        assert results[0].json() == results[1].json()


@pytest.mark.skipif(not POSTGRES_URL, reason="Concurrent mutations require PostgreSQL")
def test_concurrent_initial_requests_accept_only_one_product_chain(tmp_path, claude):
    app = make_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        sign_in(client)
        body = plan()
        project_id = post(client, "projects", body).json()["id"]
        barrier = Barrier(2)

        def create(index):
            barrier.wait()
            return post(client, f"projects/{project_id}/runs", {**body, "stageId": "understand", "goal": f"Goal {index}"})

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(create, range(2)))
        assert sorted(result.status_code for result in results) == [201, 409]
        accepted = next(result for result in results if result.status_code == 201)
        wait_run(client, project_id, accepted.json()["id"])

        async def count():
            async with app.state.database.session() as session:
                return await session.scalar(select(func.count()).select_from(ProductRun).where(ProductRun.project_id == project_id))

        assert client.portal.call(count) == 1
        assert len(claude.prompts) == 1
