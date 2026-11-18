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


def test_unverified_admin_email_does_not_grant_role(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        admin = sign_in(client, ADMIN)
        me = client.get("/api/auth/me").json()
        response = client.get("/api/admin/overview")

    assert admin["role"] == "user" and me["role"] == "user"
    assert response.status_code == 403


def test_explicit_operator_admin_role_is_preserved(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        registered = sign_in(client, "operator@example.com")

        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, registered["id"])
                user.role = "admin"
                await session.commit()

        client.portal.call(grant)
        assert client.get("/api/auth/me").json()["role"] == "admin"
        assert client.get("/api/admin/overview").status_code == 200


def test_policy_management_defaults_disabled(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        registered = sign_in(client, ADMIN)
        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, registered["id"])
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        response = client.patch("/api/admin/v1/users/target/ai-access", json={
            "frozen": True, "reason": "private", "expected_version": 0,
        }, headers={"X-CoreTeams-Admin": "1", "Idempotency-Key": "test"})
        assert response.status_code == 503


def test_policy_http_contract_auth_privacy_and_replay(tmp_path, caplog) -> None:
    app = make_app(tmp_path)
    app.state.settings.admin_policy_management_enabled = True
    path = "/api/admin/v1/users/target/ai-access"
    headers = {"X-CoreTeams-Admin": "1", "Idempotency-Key": "first"}
    sentinel = "PRIVATE-REASON-NEVER-PUBLIC"
    with TestClient(app) as client:
        assert client.get("/api/admin/v1/users").status_code == 401
        assert client.patch(path, json={}, headers=headers).status_code == 401
        target = sign_in(client, "target@example.test")
        assert client.get("/api/admin/v1/users").status_code == 403
        assert client.get(f"/api/admin/v1/users/{target['id']}/audit").status_code == 403
        assert client.patch(path, json={}, headers=headers).status_code == 403
        client.cookies.clear()
        operator = sign_in(client, "operator@example.test")
        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, operator["id"])
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        path = f"/api/admin/v1/users/{target['id']}/ai-access"
        body = {"frozen": True, "reason": sentinel, "expected_version": 0}
        for extra in ({}, {"X-CoreTeams-Admin": "1", "Idempotency-Key": "a", "Origin": "https://evil.test"}):
            assert client.patch(path, json=body, headers=extra).status_code == 403
        missing_key = client.patch(path, json=body, headers={"X-CoreTeams-Admin": "1"})
        assert missing_key.status_code == 422
        for invalid in (True, -1, "0"):
            response = client.patch(path, json={**body, "expected_version": invalid}, headers=headers)
            assert response.status_code == 422 and sentinel not in response.text
        for reason in ("\t", "\n", " ", "x" * 1001):
            response = client.patch(path, json={**body, "reason": reason}, headers=headers)
            assert response.status_code == 422
        first = client.patch(path, json=body, headers=headers)
        replay = client.patch(path, json=body, headers=headers)
        assert first.status_code == 200 and first.json() == replay.json()
        assert sentinel not in first.text
        assert first.json()["user"]["ai_access"] == {"frozen": True, "version": 1}
        conflict = client.patch(path, json={**body, "reason": "changed"}, headers=headers)
        assert conflict.status_code == 409 and sentinel not in conflict.text
        stale = client.patch(path, json=body, headers={**headers, "Idempotency-Key": "stale"})
        assert stale.status_code == 409
        assert stale.json()["current"] == {"frozen": True, "version": 1}
        own = client.patch(f"/api/admin/v1/users/{operator['id']}/ai-access", json=body, headers=headers)
        assert own.status_code == 403
        audit = client.get(f"/api/admin/v1/users/{target['id']}/audit")
        assert audit.status_code == 200 and audit.json()["total"] == 1
        assert audit.json()["items"][0]["reason"] == sentinel
        users = client.get("/api/admin/v1/users", params={"q": "TARGET@EXAMPLE"})
        assert users.status_code == 200 and users.json()["total"] == 1
        assert sentinel not in users.text
        assert users.headers["cache-control"] == "no-store"
        client.cookies.clear()
        from backend.tests.auth_helpers import PASSWORD
        login = client.post("/api/auth/login", json={"email": "target@example.test", "password": PASSWORD})
        assert login.status_code == 200
        assert login.json()["id"] == target["id"]
        me = client.get("/api/auth/me")
        assert me.status_code == 200 and sentinel not in me.text
        assert client.get("/api/admin/v1/users").status_code == 403
    assert sentinel not in caplog.text


def test_policy_get_validation_has_domain_shape_without_echo(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        operator = sign_in(client, "validation@example.test")
        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, operator["id"])
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        for path in ("/api/admin/v1/users", f"/api/admin/v1/users/{operator['id']}/audit"):
            for query in ({"page": "private-invalid"}, {"page": 0}, {"page_size": 101}):
                response = client.get(path, params=query)
                assert response.status_code == 422
                assert response.json()["code"] == "invalid_pagination"
                assert "private-invalid" not in response.text
                assert response.headers["cache-control"] == "no-store"


def test_policy_http_audit_failure_rolls_back_without_private_echo(tmp_path, monkeypatch, caplog) -> None:
    from sqlalchemy.exc import IntegrityError
    from backend.models import AiPolicyAudit
    from sqlalchemy.ext.asyncio import AsyncSession
    app = make_app(tmp_path)
    app.state.settings.admin_policy_management_enabled = True
    sentinel = "PRIVATE-FAILED-AUDIT"
    with TestClient(app) as client:
        target = sign_in(client, "rollback-target@example.test")
        client.cookies.clear()
        actor = sign_in(client, "rollback-admin@example.test")
        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, actor["id"])
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        original_flush = AsyncSession.flush
        async def fail_audit(self, *args, **kwargs):
            if any(isinstance(item, AiPolicyAudit) for item in self.new):
                raise IntegrityError("insert", {"reason": sentinel}, ValueError("audit failure"))
            return await original_flush(self, *args, **kwargs)
        monkeypatch.setattr(AsyncSession, "flush", fail_audit)
        response = client.patch(f"/api/admin/v1/users/{target['id']}/ai-access",
            json={"frozen": True, "reason": sentinel, "expected_version": 0},
            headers={"X-CoreTeams-Admin": "1", "Idempotency-Key": "audit-fail"})
        assert response.status_code == 503 and sentinel not in response.text
        async def stored():
            async with app.state.database.session() as session:
                user = await session.get(User, target["id"])
                from sqlalchemy import func
                count = await session.scalar(select(func.count()).select_from(AiPolicyAudit))
                return user.ai_frozen, user.ai_policy_version, count
        assert client.portal.call(stored) == (False, 0, 0)
    assert sentinel not in caplog.text


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
        admin = sign_in(client, ADMIN)

        async def seed():
            now = datetime.now(UTC)
            async with app.state.database.session() as session:
                user = await session.get(User, admin["id"])
                user.role = "admin"
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
