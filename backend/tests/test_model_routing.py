import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from backend import main, model_routing
from backend.config import Settings
from backend.model_routing import Check, GatewayModels, HEALTH_TTL, ModelRoutingError, provider_failure
from backend.models import AiModelHealth, Run, User
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import FakeProcess, claude_lines, make_database, write_tool_events
from backend.tests.test_product_api import plan, post, wait_run

ROUTING = "/api/admin/v1/model-routing"
HEADERS = {"X-CoreTeams-Admin": "1"}
PRIMARY = "claude-5.6-terra"
RESERVE = "glm-5.3"


class FakeGateway:
    def __init__(self):
        self.results = {PRIMARY: Check("available", reported_model=PRIMARY),
                        RESERVE: Check("available", reported_model=RESERVE)}

    async def catalog(self):
        return [PRIMARY, RESERVE, "glm-5.2", "claude-glm-5-3", "cogvideo"]

    async def check(self, model):
        return self.results.get(model, Check("incompatible", "tools_unsupported"))


def make_app(tmp_path):
    app = main.create_app(make_database(tmp_path), Settings(
        workspace_root=tmp_path / "workspaces",
        ai_model_routing_enabled=True, ai_model_primary=PRIMARY, ai_model_reserve=RESERVE,
        anthropic_base_url="https://gateway.example/anthropic", anthropic_auth_token="test-service-token",
    ))
    gateway = FakeGateway()
    app.state.model_routing.gateway = gateway
    return app, gateway


def login_admin(client, app):
    admin = sign_in(client)

    async def grant():
        async with app.state.database.session() as session:
            user = await session.get(User, admin["id"])
            user.role = "admin"
            await session.commit()

    client.portal.call(grant)


def check(client, model=None):
    response = client.post(ROUTING + "/check", json={"model": model} if model else None,
                           headers={**HEADERS, "Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    return response.json()


def configure(client, settings, **change):
    return client.patch(ROUTING, headers=HEADERS, json={
        "primary_model": settings["primary_model"], "reserve_model": settings["reserve_model"],
        "paused": settings["paused"], "expected_version": settings["version"], **change,
    })


def project(client):
    body = plan()
    project_id = post(client, "projects", body).json()["id"]
    return project_id, {**body, "stageId": "understand", "fileIds": []}


def test_fallback_closes_new_runs_and_pause_survives_recovery(tmp_path, monkeypatch):
    app, gateway = make_app(tmp_path)
    used = []

    async def start(prompt, workspace, session=None, *, execution_model, gateway_env, permission_mode):
        assert gateway_env["ANTHROPIC_AUTH_TOKEN"] == "test-service-token"
        used.append(execution_model)
        lines = claude_lines("Done")
        lines[1:1] = write_tool_events("coreteams/understand.md", "# Fallback document")
        return FakeProcess(lines)

    monkeypatch.setattr(main, "start_claude", start)
    with TestClient(app) as client:
        assert client.get(ROUTING).status_code == 401
        login_admin(client, app)
        assert check(client)["active_model"] == PRIMARY
        gateway.results[PRIMARY] = Check("unavailable", "model_unavailable")
        fallback = check(client)
        assert fallback["active_model"] == RESERVE
        first_id, first_body = project(client)
        first = post(client, f"projects/{first_id}/runs", first_body)
        assert first.status_code == 201, first.text
        final = wait_run(client, first_id, first.json()["id"])
        assert final["status"] == "completed" and final["artifacts"][0]["content"] == "# Fallback document"
        assert used == [RESERVE]

        gateway.results[PRIMARY] = Check("unavailable", "access_rejected")
        gateway.results[RESERVE] = Check("unavailable", "access_rejected")
        stopped = check(client)
        assert stopped["active_model"] is None and stopped["reason"] == "models_unavailable"
        gateway.results[PRIMARY] = gateway.results[RESERVE] = Check("unavailable", "gateway_unreachable")
        assert check(client)["reason"] == "gateway_unavailable"
        second_id, second_body = project(client)
        failed = post(client, f"projects/{second_id}/runs", second_body)
        assert failed.status_code == 503 and failed.json()["code"] == "ai_models_unavailable"

        paused = configure(client, stopped, paused=True)
        assert paused.status_code == 200, paused.text
        gateway.results[PRIMARY] = Check("available", reported_model=PRIMARY)
        gateway.results[RESERVE] = Check("available", reported_model=RESERVE)
        assert check(client)["reason"] == "paused"
        failed = post(client, f"projects/{second_id}/runs", second_body)
        assert failed.status_code == 503 and failed.json()["code"] == "ai_launches_paused"
        assert configure(client, paused.json(), paused=False).json()["active_model"] == PRIMARY

        audit = client.get(ROUTING + "/history").json()
        check(client)
        assert client.get(ROUTING + "/history").json()["total"] == audit["total"]
        assert any(entry["action"] == "automatic" and entry["after"]["active_model"] == RESERVE
                   for entry in audit["items"])


def test_refresh_evicts_routes_that_lose_tool_support(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    gateway = GatewayModels("https://gateway.example/anthropic", "test-token")
    app.state.model_routing.gateway = gateway
    tool_support = {PRIMARY, RESERVE}

    async def respond(method, path, *, json):
        model = json["model"]
        content = [{"type": "text", "text": "OK"}]
        if model in tool_support and "tool_choice" in json:
            content = [{"type": "tool_use", "name": json["tool_choice"]["name"]}]
        return {"type": "message", "model": model, "content": content}

    monkeypatch.setattr(gateway, "_request", respond)
    with TestClient(app) as client:
        login_admin(client, app)
        assert check(client)["active_model"] == PRIMARY

        tool_support.remove(PRIMARY)
        fallback = check(client)
        assert fallback["active_model"] == RESERVE
        assert {model["id"]: model["state"] for model in fallback["models"]} == {
            PRIMARY: "incompatible", RESERVE: "available",
        }

        tool_support.remove(RESERVE)
        assert check(client)["active_model"] is None
        project_id, body = project(client)
        response = post(client, f"projects/{project_id}/runs", body)
        assert response.status_code == 503
        assert response.json()["code"] == "ai_models_unavailable"

        tool_support.add(PRIMARY)
        assert check(client)["active_model"] == PRIMARY


def test_catalog_verification_version_and_access(tmp_path):
    app, gateway = make_app(tmp_path)
    gateway.results["glm-5.2"] = Check("available", reported_model="glm-5.2")
    gateway.results["claude-glm-5-3"] = Check("available", reported_model=RESERVE)
    with TestClient(app) as client:
        sign_in(client, "ordinary@example.com")
        assert client.get(ROUTING).status_code == 403
        login_admin(client, app)
        initial = check(client)
        assert client.get(ROUTING + "/catalog?q=glm-5.2").json()["items"] == ["glm-5.2"]
        assert configure(client, initial, reserve_model="glm-5.2").status_code == 422
        assert client.patch(ROUTING, json={}).status_code == 422
        refused = client.post(ROUTING + "/check", json={"model": "glm-5.2"},
                              headers={**HEADERS, "Origin": "https://untrusted.example"})
        assert refused.status_code == 403
        state = check(client, "glm-5.2")
        assert client.get(ROUTING + "/catalog?q=glm-5.2").json()["items"] == []
        assert next(item for item in state["models"] if item["id"] == "glm-5.2")["state"] == "available"
        duplicate = configure(client, check(client, "claude-glm-5-3"), primary_model="claude-glm-5-3")
        assert duplicate.status_code == 422 and duplicate.json()["code"] == "duplicate_model"
        check(client, "cogvideo")
        assert configure(client, state, reserve_model="cogvideo").status_code == 422
        changing = configure(client, state, reserve_model="glm-5.2")
        assert changing.status_code == 200
        assert configure(client, state, paused=True).status_code == 409
        assert client.get(ROUTING).json()["reserve_model"] == "glm-5.2"


def failing_run(*, wrote=False, failure="authentication"):
    events = [json.dumps({"type": "system", "subtype": "init", "session_id": "session-1"})]
    if wrote:
        events += write_tool_events("coreteams/understand.md", "# Saved before interruption")
    result = {"type": "result", "is_error": True, "session_id": "session-1"}
    if failure == "http-status":
        result["api_error_status"] = 429
    else:
        network = failure == "connection"
        events.append(json.dumps({
            "type": "assistant", "error": "unknown" if network else "authentication_failed",
            "message": {"content": [{"type": "text", "text": (
                "API Error: Unable to connect to API (ECONNREFUSED)" if network else "Private gateway diagnostic"
            )}]},
        }))
    events.append(json.dumps(result))
    return FakeProcess(events, exit_code=1)


@pytest.mark.parametrize("failure", ["authentication", "connection", "http-status"])
def test_retry_preserves_facilitator_answer_and_uses_new_route(tmp_path, monkeypatch, failure):
    app, gateway = make_app(tmp_path)
    calls = []
    question = {
        "type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "ask-1", "name": "AskUserQuestion", "input": {"questions": [
                {"question": "What color?", "header": "Color", "options": [{"label": "Blue"}]},
            ]}},
        ]},
    }

    async def start(prompt, workspace, session=None, *, execution_model, gateway_env, permission_mode):
        calls.append((prompt, session, execution_model))
        if len(calls) == 1:
            return FakeProcess([claude_lines("Open question")[0], json.dumps(question), json.dumps({
                "type": "result", "is_error": False, "session_id": "session-1",
                "permission_denials": [{"tool_name": "AskUserQuestion"}],
            })])
        if len(calls) == 2:
            return failing_run(failure=failure)
        lines = claude_lines("Done")
        lines[1:1] = write_tool_events("coreteams/understand.md", "# Result on GLM")
        return FakeProcess(lines)

    monkeypatch.setattr(main, "start_claude", start)
    with TestClient(app) as client:
        login_admin(client, app)
        initial_check = check(client)["last_checked_at"]
        project_id, body = project(client)
        created = post(client, f"projects/{project_id}/runs", body)
        run_id = created.json()["id"]
        waiting = wait_run(client, project_id, run_id)
        assert waiting["status"] == "waiting_for_input"
        gateway.results[PRIMARY] = Check("unavailable", "access_rejected")
        prefix = f"projects/{project_id}/runs/{run_id}"
        answered = post(client, prefix + "/answers", {"questionId": waiting["question"]["id"], "answers": {"What color?": "Blue"}})
        assert answered.status_code == 200, answered.text
        failed = wait_run(client, project_id, run_id)
        assert failed["errorCode"] == "ai_model_request_failed"
        assert "Private gateway diagnostic" not in json.dumps(failed)
        assert "ECONNREFUSED" not in json.dumps(failed)
        deadline = time.monotonic() + 8
        while client.get(ROUTING).json()["last_checked_at"] == initial_check:
            assert time.monotonic() < deadline, "The CLI provider error did not trigger an out-of-band check"
            time.sleep(0.04)
        assert client.get(ROUTING).json()["active_model"] == RESERVE
        key = str(uuid4())
        retry_body = {"attemptId": failed["retryAttemptId"]}
        retry = post(client, prefix + "/retry", retry_body, key)
        assert retry.status_code == 200, retry.text
        result = wait_run(client, project_id, run_id)
        assert result["status"] == "completed" and result["retryCount"] == 1
        assert result["artifacts"][0]["content"] == "# Result on GLM"
        assert calls[1:] == [("Blue", "session-1", PRIMARY), ("Blue", "session-1", RESERVE)]
        assert post(client, prefix + "/retry", retry_body, key).json() == result
        assert post(client, prefix + "/retry", retry_body).status_code == 409

        async def execution_models():
            async with app.state.database.session() as session:
                return list(await session.scalars(select(Run.execution_model).order_by(Run.started_at)))

        assert client.portal.call(execution_models) == [PRIMARY, PRIMARY, RESERVE]


def test_provider_failure_blocks_retry_on_primary_and_invalidates_inflight_probe(tmp_path, monkeypatch):
    app, gateway = make_app(tmp_path)
    routing = app.state.model_routing
    used_models = []
    queued_checks = []
    probe_started, probe_release = asyncio.Event(), asyncio.Event()
    original_check = gateway.check

    async def stale_probe(model):
        result = await original_check(model)
        if model == PRIMARY:
            probe_started.set()
            await asyncio.wait_for(probe_release.wait(), 10)
        return result

    async def start(*args, execution_model, **kwargs):
        used_models.append(execution_model)
        if execution_model == PRIMARY:
            return failing_run(failure="http-status")
        return FakeProcess(claude_lines("Done"))

    monkeypatch.setattr(main, "start_claude", start)
    monkeypatch.setattr(routing, "request_check", lambda: queued_checks.append(True))
    with TestClient(app) as client:
        login_admin(client, app)
        assert check(client)["active_model"] == PRIMARY
        monkeypatch.setattr(gateway, "check", stale_probe)
        in_flight = client.portal.start_task_soon(routing.refresh)
        client.portal.call(asyncio.wait_for, probe_started.wait(), 10)

        project_id, body = project(client)
        prefix = f"projects/{project_id}/runs"
        created = post(client, prefix, body)
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        failed = wait_run(client, project_id, run_id)
        assert failed["errorCode"] == "ai_model_request_failed"
        assert client.get(ROUTING).json()["active_model"] == RESERVE

        retry = post(client, f"{prefix}/{run_id}/retry", {"attemptId": failed["retryAttemptId"]})
        assert retry.status_code == 200, retry.text
        assert wait_run(client, project_id, run_id)["status"] == "completed"
        assert used_models == [PRIMARY, RESERVE]
        assert queued_checks == [True]

        client.portal.call(probe_release.set)
        in_flight.result(timeout=10)
        assert client.get(ROUTING).json()["active_model"] == RESERVE

        monkeypatch.setattr(gateway, "check", original_check)
        assert check(client)["active_model"] == PRIMARY


def test_interruption_after_tool_has_no_retry(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)

    async def start(*args, **kwargs):
        return failing_run(wrote=True)

    monkeypatch.setattr(main, "start_claude", start)
    with TestClient(app) as client:
        login_admin(client, app)
        check(client)
        project_id, body = project(client)
        created = post(client, f"projects/{project_id}/runs", body)
        run_id = created.json()["id"]
        failed = wait_run(client, project_id, run_id, statuses=("outcome_unknown",))
        assert failed["errorCode"] == "ai_model_interrupted" and "retryAttemptId" not in failed
        assert failed["artifacts"][0]["content"] == "# Saved before interruption"
        assert post(client, f"projects/{project_id}/runs/{run_id}/retry", {"attemptId": run_id}).status_code == 409


def test_stale_health_fails_closed_without_waiting_for_worker(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        login_admin(client, app)
        check(client)

        async def age_checks():
            async with app.state.database.session() as session:
                await session.execute(update(AiModelHealth).values(checked_at=datetime.now(UTC) - HEALTH_TTL - timedelta(seconds=2)))
                await session.commit()

        client.portal.call(age_checks)
        assert client.get(ROUTING).json()["active_model"] is None
        project_id, body = project(client)
        assert post(client, f"projects/{project_id}/runs", body).json()["code"] == "ai_models_unavailable"


async def test_gateway_qualification_requires_a_real_tool_call(monkeypatch):
    gateway = GatewayModels("https://gateway.example/anthropic", "test-token")
    seen = []

    async def respond(method, path, **kwargs):
        seen.append((method, path, kwargs))
        model = kwargs["json"]["model"]
        content = [{"type": "text", "text": "OK"}]
        if model == RESERVE:
            content = [{"type": "tool_use", "name": kwargs["json"]["tool_choice"]["name"]}]
        return {"type": "message", "model": model, "content": content}

    monkeypatch.setattr(gateway, "_request", respond)
    assert (await gateway.check("not-a-tool-model")).state == "incompatible"
    assert seen[0][2]["json"]["tool_choice"]["type"] == "tool"
    assert (await gateway.check(RESERVE)).state == "available"


async def test_catalog_deadline_covers_pagination_and_releases_lock(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    service = app.state.model_routing
    service.gateway = GatewayModels("https://gateway.example/anthropic", "test-token")
    monkeypatch.setattr(model_routing, "REQUEST_TIMEOUT", 0.04)
    requests = []

    async def pages(method, path, *, params):
        requests.append(params)
        if "after_id" in params:
            await asyncio.Event().wait()
        return {"data": [{"id": PRIMARY}], "last_id": PRIMARY, "has_more": True}

    monkeypatch.setattr(service.gateway, "_request", pages)
    with pytest.raises(ModelRoutingError) as error:
        await asyncio.wait_for(service.catalog(), timeout=0.5)
    assert error.value.code == "model_catalog_unavailable"
    assert requests == [{}, {"after_id": PRIMARY}]

    async def healthy(method, path, **kwargs):
        return {"data": [{"id": RESERVE}], "has_more": False}

    monkeypatch.setattr(service.gateway, "_request", healthy)
    assert await service.catalog() == [RESERVE]
    await app.state.database.dispose()


def test_request_validation_errors_do_not_mark_provider_unavailable():
    assert not provider_failure("unknown", "API Error: 400 invalid request", http_status=400)
    assert not provider_failure("unknown", "Unexpected application error")
