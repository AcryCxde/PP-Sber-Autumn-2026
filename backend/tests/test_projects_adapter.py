import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.config import Settings
from backend.db import Database


def make_app(tmp_path: Path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'history.db').as_posix()}")
    return main_module.create_app(
        database,
        Settings(
            workspace_root=tmp_path / "workspaces",
            max_concurrent_runs=1,
            subscription_window_seconds=0.01,
            subscription_poll_interval_seconds=0.01,
        ),
    )


def test_project_bridge_is_idempotent_and_rejects_unmodelled_project_fields(tmp_path):
    app = make_app(tmp_path)
    body = {"clientProjectId": "project-1", "title": "Bridge task"}
    with TestClient(app) as client:
        created = client.post("/api/projects", json=body)
        assert created.status_code == 201
        assert created.json() == {"id": "project-1", "bridge": "legacy_conversation"}
        assert client.post("/api/projects", json=body).status_code == 201
        assert client.post("/api/projects", json={**body, "title": "Other"}).status_code == 409
        assert client.post("/api/projects", json={**body, "files": []}).status_code == 422


def test_run_bridge_uses_legacy_acceptance_and_exposes_pollable_snapshot(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    executed: list[str] = []

    async def fake_handle(_sink, **kwargs):
        executed.append(kwargs["client_request_id"])

    monkeypatch.setattr(main_module, "handle_run_create", fake_handle)
    with TestClient(app) as client:
        assert client.post("/api/projects", json={"clientProjectId": "project-2", "title": "Task"}).status_code == 201
        missing_key = client.post("/api/projects/project-2/runs", json={"content": "Decide"})
        assert missing_key.status_code == 400
        headers = {"Idempotency-Key": "request-2"}
        created = client.post("/api/projects/project-2/runs", json={"content": "Decide"}, headers=headers)
        assert created.status_code == 200
        first = created.json()
        assert first["status"] == "queued"
        assert first["stageId"] == "legacy-conversation"
        assert first["artifacts"] == []
        replay = client.post("/api/projects/project-2/runs", json={"content": "Decide"}, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["id"] == first["id"]
        fetched = client.get(f"/api/projects/project-2/runs/{first['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["messages"][0]["text"] == "Decide"
        assert client.get(f"/api/projects/other/runs/{first['id']}").status_code == 404

    # Let the scheduled fake handler release the shared executor slot.
    asyncio.run(asyncio.sleep(0))
    assert executed == ["request-2"]


def test_run_bridge_respects_legacy_executor_limit(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/projects", json={"clientProjectId": "project-3", "title": "Task"}).status_code == 201
        app.state.run_task_count = 1
        response = client.post(
            "/api/projects/project-3/runs",
            json={"content": "Decide"},
            headers={"Idempotency-Key": "request-3"},
        )
        assert response.status_code == 429


def test_run_bridge_allows_idempotent_replay_at_capacity(tmp_path, monkeypatch):
    app = make_app(tmp_path)

    async def fake_handle(_sink, **_kwargs):
        return None

    monkeypatch.setattr(main_module, "handle_run_create", fake_handle)
    with TestClient(app) as client:
        assert client.post("/api/projects", json={"clientProjectId": "project-4", "title": "Task"}).status_code == 201
        headers = {"Idempotency-Key": "request-4"}
        assert client.post("/api/projects/project-4/runs", json={"content": "Decide"}, headers=headers).status_code == 200
        app.state.run_task_count = app.state.settings.max_concurrent_runs
        assert client.post("/api/projects/project-4/runs", json={"content": "Decide"}, headers=headers).status_code == 200
