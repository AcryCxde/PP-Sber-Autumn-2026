"""Негативная матрица изоляции пользователей.

Перенос сценариев PPS-132 (ветка isolation-matrix-storage, web-demo) на FastAPI-бэкенд:
пользователь вместо заголовка x-demo-user определяется по сессии, «проект» — это диалог.
Каждая проверка сначала подтверждает доступ владельца, затем отказ чужому: чужое неотличимо
от несуществующего (404 / *_not_found) и не меняет состояние.
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import backend.main as main_module
from backend.auth import SESSION_COOKIE
from backend.models import Conversation, Run
from backend.tests.auth_helpers import PASSWORD, sign_in
from backend.tests.test_history_api import (
    FakeProcess,
    claude_lines,
    make_app,
    run_command,
    write_tool_events,
)


ALPHA = "alpha@example.com"
BETA = "beta@example.com"
SECRET_CONTENT = "alpha secret artifact"


@dataclass
class AlphaRun:
    conversation_id: str
    run_id: str
    client_request_id: str
    artifact_seq: int


def switch_user(client: TestClient, email: str) -> None:
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def receive_until_terminal(
    websocket,
    stop: frozenset[str] = frozenset({"run.completed", "run.failed", "command.error"}),
) -> list[dict]:
    events = []
    while True:
        event = websocket.receive_json()
        events.append(event)
        if event["type"] in stop:
            return events


REPLAY_STOP = frozenset({"replay.complete", "command.error"})


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    lines = claude_lines("Alpha answer")
    lines[1:1] = write_tool_events("secret.txt", SECRET_CONTENT)

    async def fake_start_claude(prompt, workspace, resume_session_id=None, *, permission_mode=None):
        del prompt, workspace, resume_session_id, permission_mode
        return FakeProcess(list(lines))

    monkeypatch.setattr(main_module, "start_claude", fake_start_claude)
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client, BETA)
        client.cookies.clear()
        sign_in(client, ALPHA)
        command = run_command("Alpha private request")
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(command)
            events = receive_until_terminal(websocket)
        assert events[-1]["type"] == "run.completed"
        artifact = next(event for event in events if event["type"] == "artifact.saved")
        alpha = AlphaRun(
            conversation_id=events[0]["conversation_id"],
            run_id=events[0]["run_id"],
            client_request_id=command["client_request_id"],
            artifact_seq=artifact["seq"],
        )
        yield app, client, alpha


def alpha_routes(alpha: AlphaRun) -> list[tuple[str, str]]:
    return [
        ("GET", f"/api/conversations/{alpha.conversation_id}/messages"),
        ("GET", f"/api/conversations/{alpha.conversation_id}/events"),
        ("GET", f"/api/runs/{alpha.run_id}/events"),
        ("GET", f"/api/runs/{alpha.run_id}/events/{alpha.artifact_seq}/file"),
    ]


def test_each_user_sees_only_own_conversations(isolated) -> None:
    _, client, alpha = isolated

    own = client.get("/api/conversations").json()
    switch_user(client, BETA)
    foreign = client.get("/api/conversations").json()

    assert [item["id"] for item in own] == [alpha.conversation_id]
    assert foreign == []


def test_foreign_history_reads_are_hidden_after_owner_positive_control(isolated) -> None:
    _, client, alpha = isolated

    for method, route in alpha_routes(alpha):
        assert client.request(method, route).status_code == 200, route

    switch_user(client, BETA)
    for method, route in alpha_routes(alpha):
        foreign = client.request(method, route)
        missing = client.request(method, route.replace(alpha.run_id, str(uuid4())).replace(
            alpha.conversation_id, str(uuid4())
        ))
        assert foreign.status_code == 404, route
        # Чужое и несуществующее неотличимы.
        assert foreign.json() == missing.json(), route
        assert SECRET_CONTENT.encode() not in foreign.content


def test_foreign_conversation_cannot_be_deleted(isolated) -> None:
    app, client, alpha = isolated

    switch_user(client, BETA)
    assert client.delete(f"/api/conversations/{alpha.conversation_id}").status_code == 404

    switch_user(client, ALPHA)
    assert [item["id"] for item in client.get("/api/conversations").json()] == [
        alpha.conversation_id
    ]


def test_foreign_conversation_cannot_be_continued_over_websocket(isolated) -> None:
    app, client, alpha = isolated

    switch_user(client, BETA)
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(run_command("Inject", conversation_id=alpha.conversation_id))
        [error] = receive_until_terminal(websocket)

    async def alpha_runs() -> int:
        async with app.state.database.session() as session:
            return await session.scalar(
                select(func.count(Run.id)).where(Run.conversation_id == alpha.conversation_id)
            )

    assert error["type"] == "command.error"
    assert error["code"] == "conversation_not_found"
    assert client.portal.call(alpha_runs) == 1


def test_foreign_client_request_id_does_not_replay_alpha_run(isolated) -> None:
    _, client, alpha = isolated

    switch_user(client, BETA)
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(
            run_command("Alpha private request", client_request_id=alpha.client_request_id)
        )
        [error] = receive_until_terminal(websocket)

    assert error["type"] == "command.error"
    assert error["code"] == "idempotency_conflict"
    assert alpha.run_id not in str(error) and alpha.conversation_id not in str(error)


def test_foreign_run_subscription_is_rejected(isolated) -> None:
    _, client, alpha = isolated

    with client.websocket_connect("/ws") as websocket:
        websocket.send_json({"type": "run.subscribe", "run_id": alpha.run_id, "after_seq": 0})
        own = receive_until_terminal(websocket, REPLAY_STOP)

    switch_user(client, BETA)
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json({"type": "run.subscribe", "run_id": alpha.run_id, "after_seq": 0})
        [foreign] = receive_until_terminal(websocket, REPLAY_STOP)

    assert own[-1]["type"] == "replay.complete"
    assert foreign["code"] == "run_not_found"


def test_new_conversation_is_owned_by_socket_user(isolated) -> None:
    app, client, alpha = isolated

    switch_user(client, BETA)
    beta_id = client.get("/api/auth/me").json()["id"]
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(run_command("Beta request"))
        events = receive_until_terminal(websocket)

    async def owner_of(conversation_id: str) -> str | None:
        async with app.state.database.session() as session:
            conversation = await session.get(Conversation, conversation_id)
            return conversation.owner_id

    assert events[-1]["type"] == "run.completed"
    assert client.portal.call(owner_of, events[0]["conversation_id"]) == beta_id
    assert events[0]["conversation_id"] != alpha.conversation_id


def test_forged_session_cookie_is_rejected(isolated) -> None:
    _, client, _ = isolated

    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, "forged-token")

    assert client.get("/api/conversations").status_code == 401


def test_legacy_conversation_without_owner_is_hidden_from_everyone(isolated) -> None:
    app, client, _ = isolated

    async def legacy() -> str:
        async with app.state.database.session() as session:
            conversation = Conversation(title="Legacy", owner_id=None)
            session.add(conversation)
            await session.commit()
            return conversation.id

    legacy_id = client.portal.call(legacy)
    for email in (ALPHA, BETA):
        switch_user(client, email)
        assert legacy_id not in [item["id"] for item in client.get("/api/conversations").json()]
        assert client.get(f"/api/conversations/{legacy_id}/messages").status_code == 404
