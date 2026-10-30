from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

import backend.main as main_module
from backend.config import Settings
from backend.models import Artifact, Conversation, Message, Run, User, UserActivityDay
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import make_database


ADMIN = "boss@example.com"


def make_app(tmp_path):
    return main_module.create_app(
        make_database(tmp_path),
        Settings(workspace_root=tmp_path / "workspaces", admin_emails=f" {ADMIN.upper()} , other@example.com"),
    )


def test_admin_endpoints_require_admin_role(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        anonymous = client.get("/api/admin/overview")
        user = sign_in(client, "alpha@example.com")
        forbidden = [client.get(path).status_code for path in ("/api/admin/overview", "/api/admin/users")]

    assert anonymous.status_code == 401
    assert user["role"] == "user"
    assert forbidden == [403, 403]


def test_admin_email_is_promoted_on_register(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        admin = sign_in(client, ADMIN)
        me = client.get("/api/auth/me").json()
        response = client.get("/api/admin/overview")

    assert admin["role"] == "admin" and me["role"] == "admin"
    assert response.status_code == 200


def test_requests_record_one_activity_day(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client, "alpha@example.com")
        for _ in range(3):
            assert client.get("/api/auth/me").status_code == 200

        async def stored():
            async with app.state.database.session() as session:
                return await session.scalar(select(User)), list(await session.scalars(select(UserActivityDay)))

        user, days = client.portal.call(stored)

    assert user.last_seen_at is not None
    assert [day.day for day in days] == [datetime.now(UTC).date()]


def test_overview_and_users_aggregate_engagement(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        alpha = sign_in(client, "alpha@example.com")
        client.cookies.clear()
        sign_in(client, "idle@example.com")
        client.cookies.clear()
        sign_in(client, ADMIN)

        async def seed():
            now = datetime.now(UTC)
            async with app.state.database.session() as session:
                conversation = Conversation(owner_id=alpha["id"], title="Задача")
                session.add(conversation)
                await session.flush()
                done = Run(
                    conversation_id=conversation.id,
                    client_request_id="r1",
                    status="completed",
                    started_at=now,
                    completed_at=now,
                    duration_ms=1500,
                    cost_usd=Decimal("0.25"),
                )
                failed = Run(
                    conversation_id=conversation.id,
                    client_request_id="r2",
                    status="failed",
                    started_at=now,
                )
                session.add_all([done, failed])
                await session.flush()
                session.add_all(
                    [
                        Message(conversation_id=conversation.id, run_id=done.id, role="user", sequence=0, content="a"),
                        Message(conversation_id=conversation.id, run_id=done.id, role="assistant", sequence=1, content="b"),
                        Message(conversation_id=conversation.id, run_id=failed.id, role="user", sequence=2, content="c"),
                        Artifact(run_id=done.id, event_seq=1, path="plan.md", content="# План"),
                        # Активность в прошлом: alpha возвращался.
                        UserActivityDay(user_id=alpha["id"], day=now.date() - timedelta(days=3)),
                    ]
                )
                await session.commit()

        client.portal.call(seed)
        overview = client.get("/api/admin/overview", params={"days": 7})
        users = client.get("/api/admin/users")
        too_short = client.get("/api/admin/overview", params={"days": 1})

    assert overview.status_code == 200, overview.text
    body = overview.json()
    totals = body["totals"]
    assert totals["users"] == 3 and totals["admins"] == 1 and totals["new_users"] == 3
    assert totals["active_1d"] == 3 and totals["returning_period"] == 1
    assert totals["runs"] == 2 and totals["failed_runs"] == 1
    assert totals["user_messages"] == 2 and totals["artifacts"] == 1
    assert totals["cost_usd"] == 0.25 and totals["compute_seconds"] == 1.5
    assert len(body["daily"]) == 7
    assert body["daily"][-1]["runs"] == 2 and body["daily"][-1]["active_users"] == 3
    assert body["daily"][-4]["active_users"] == 1
    funnel = {step["key"]: step["users"] for step in body["funnel"]}
    assert funnel == {"registered": 3, "started": 1, "ran": 1, "result": 1, "approved": 0}

    assert users.status_code == 200
    by_email = {row["email"]: row for row in users.json()}
    assert by_email["alpha@example.com"]["runs"] == 2
    assert by_email["alpha@example.com"]["active_days"] == 2
    assert by_email["alpha@example.com"]["last_action_at"] is not None
    assert by_email["idle@example.com"]["conversations"] == 0
    assert by_email[ADMIN]["role"] == "admin"
    assert too_short.status_code == 422
