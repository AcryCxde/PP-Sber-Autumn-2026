"""Regressions for backend failures found in PR36 review."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from backend import main
from backend.ai_access import AiAccessError
from backend.config import Settings
from backend.db import Database
from backend.models import Run, User
from backend.repository import accept_run
from backend.tests.databases import database_url
from backend.tests.test_admin_operations import seed
from backend.tests.test_ai_access_ingress import setup
from backend.tests.test_history_api import make_app
from backend.tests.auth_helpers import sign_in


def test_admission_failure_of_duplicate_does_not_terminate_active_executor(tmp_path, monkeypatch):
    async def scenario():
        database = Database(database_url(tmp_path / "duplicate.db"))
        owner = await setup(database)
        async with database.session() as session:
            acceptance = await accept_run(session, conversation_id=None, client_request_id=str(uuid4()), content="test", owner_id=owner)
        entered, release = asyncio.Event(), asyncio.Event()
        async def executor(*args, **kwargs):
            entered.set()
            await release.wait()
        monkeypatch.setattr(main, "run_via_runner", executor)
        state = SimpleNamespace(database=database, settings=Settings(executor="runner"))
        delivered = []
        async def deliver(event):
            delivered.append(event.type)
        first = asyncio.create_task(main.execute_accepted_run(state, acceptance, "test", deliver))
        await entered.wait()
        async def unavailable(*args):
            raise AiAccessError("ai_policy_unavailable", 503, "unavailable")
        monkeypatch.setattr(main, "admit_run", unavailable)
        try:
            await main.execute_accepted_run(state, acceptance, "test", deliver)
            async with database.session() as session:
                run = await session.get(Run, acceptance.run_id)
                assert run.status == "running"
                assert run.admitted_at is not None
            assert delivered == ["run.started"]
        finally:
            release.set()
            await first
            await database.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [ValueError("harness_lineage_conflict"), DBAPIError("projection", {}, RuntimeError("database locked"))])
def test_optional_projection_failure_preserves_run_details(tmp_path, monkeypatch, failure):
    from backend import harness_estimate
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        ids = seed(app, client)
        app.state.runner_transport = SimpleNamespace(diagnostic_snapshots=lambda run_id: ["synthetic"])
        async def fail(*args):
            raise failure
        monkeypatch.setattr(harness_estimate, "catch_up", fail)
        response = client.get(f"/api/admin/v1/runs/{ids[1]}")
        assert response.status_code == 200
        body = response.json()
        assert body["run"]["id"] == ids[1]
        assert body["harness_estimate"]["state"] == "unknown"
        assert body["harness_estimate"]["unavailable_reason"] == "projection_unavailable"
        assert "database locked" not in response.text


def test_unrelated_projection_bug_is_not_hidden(tmp_path, monkeypatch):
    from backend import harness_estimate
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client)
        ids = seed(app, client)
        app.state.runner_transport = SimpleNamespace(diagnostic_snapshots=lambda run_id: [])
        async def fail(*args):
            raise ValueError("unexpected_programming_error")
        monkeypatch.setattr(harness_estimate, "catch_up", fail)
        with pytest.raises(ValueError, match="unexpected_programming_error"):
            client.get(f"/api/admin/v1/runs/{ids[1]}")


def test_nginx_preserves_external_authority_for_http_and_websocket():
    config = (Path(__file__).parents[2] / "frontend/nginx.conf.template").read_text()
    assert config.count("proxy_set_header Host $http_host;") == 2
    assert "proxy_set_header Host $host;" not in config


def test_external_port_origin_allowed_but_foreign_origin_rejected(tmp_path):
    app = make_app(tmp_path)
    app.state.settings.admin_policy_management_enabled = True
    with TestClient(app, base_url="http://localhost:5173") as client:
        target = sign_in(client, "target-port@example.test")
        client.cookies.clear()
        operator = sign_in(client, "operator-port@example.test")
        async def grant():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User).where(User.id == operator["id"]))
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        path = f"/api/admin/v1/users/{target['id']}/ai-access"
        headers = {"Host": "localhost:5173", "Origin": "http://localhost:5173", "X-CoreTeams-Admin": "1", "Idempotency-Key": "external-port"}
        body = {"frozen": True, "expected_version": 0, "reason": "synthetic"}
        assert client.patch(path, headers={**headers, "Origin": "http://evil.test:5173"}, json=body).status_code == 403
        assert client.patch(path, headers=headers, json=body).status_code == 200
