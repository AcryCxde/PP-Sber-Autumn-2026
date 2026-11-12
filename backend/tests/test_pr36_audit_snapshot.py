"""Audit pages and totals share one statement snapshot, including empty pages."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from backend.ai_access import change_ai_access, policy_transaction
from backend.api.admin import policy_audit
from backend.db import Database
from backend.models import AiPolicyAudit, User
from backend.tests.auth_helpers import sign_in
from backend.tests.databases import POSTGRES_URL, database_url
from backend.tests.test_admin_api import make_app


async def seed_audits(database, *, count):
    async with database.session() as session:
        actor = User(email="audit-actor@example.test", password_hash="synthetic", role="admin")
        target = User(email="audit-target@example.test", password_hash="synthetic")
        other = User(email="audit-other@example.test", password_hash="synthetic")
        session.add_all([actor, target, other])
        await session.flush()
        stamp = datetime(2026, 11, 12, 12, tzinfo=UTC)
        identifiers = [str(uuid4()) for _ in range(count)]
        for index, identifier in enumerate(identifiers):
            session.add(AiPolicyAudit(id=identifier, actor_id=actor.id, target_id=target.id,
                created_at=stamp, action="freeze", reason=f"private-{index}",
                before_version=index, after_version=index + 1,
                idempotency_key=f"seed-{index}", request_hash="0" * 64, response={}))
        session.add(AiPolicyAudit(actor_id=actor.id, target_id=other.id,
            created_at=stamp, action="freeze", reason="OTHER-TARGET-PRIVATE",
            before_version=0, after_version=1, idempotency_key="other-target",
            request_hash="0" * 64, response={}))
        await session.commit()
        return actor.id, target.id, other.id, sorted(identifiers, reverse=True)


def test_audit_pages_use_one_statement_and_preserve_order_and_empty_total(tmp_path):
    async def scenario():
        database = Database(database_url(tmp_path / "audit-page.db"))
        await database.migrate()
        try:
            _, target_id, _, identifiers = await seed_audits(database, count=3)
            statements = []

            def capture(connection, cursor, statement, parameters, context, executemany):
                if "ai_policy_audits" in statement and statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    statements.append(statement)

            event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
            try:
                for page, expected in ((1, identifiers[:2]), (2, identifiers[2:]), (3, [])):
                    statements.clear()
                    async with database.session() as session:
                        response = await policy_audit(target_id, None, session, page=page, page_size=2)
                    body = json.loads(response.body)
                    assert body["total"] == 3
                    assert [row["id"] for row in body["items"]] == expected
                    assert body["page"] == page and body["page_size"] == 2
                    assert all(row["target_id"] == target_id for row in body["items"])
                    assert "OTHER-TARGET-PRIVATE" not in response.body.decode()
                    assert response.headers["cache-control"] == "no-store"
                    assert len(statements) == 1
            finally:
                event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        finally:
            await database.dispose()
    asyncio.run(scenario())


def test_empty_audit_and_missing_user_keep_distinct_contracts(tmp_path):
    async def scenario():
        database = Database(database_url(tmp_path / "audit-empty.db"))
        await database.migrate()
        try:
            _, target_id, _, _ = await seed_audits(database, count=0)
            async with database.session() as session:
                empty = await policy_audit(target_id, None, session, page=8, page_size=25)
                missing = await policy_audit(str(uuid4()), None, session, page=1, page_size=25)
            assert json.loads(empty.body)["total"] == 0
            assert json.loads(empty.body)["items"] == []
            assert missing.status_code == 404
            assert json.loads(missing.body)["code"] == "user_not_found"
        finally:
            await database.dispose()
    asyncio.run(scenario())


def test_audit_http_remains_admin_only_and_private(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        async def seed():
            return await seed_audits(app.state.database, count=1)
        actor_id, target_id, _, _ = client.portal.call(seed)
        path = f"/api/admin/v1/users/{target_id}/audit"
        assert client.get(path).status_code == 401
        ordinary = sign_in(client, "audit-reader@example.test")
        denied = client.get(path)
        assert denied.status_code == 403 and "private-0" not in denied.text
        assert "private-0" not in client.get("/api/auth/me").text

        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, ordinary["id"])
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        response = client.get(path)
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["actor_id"] == actor_id and item["reason"] == "private-0"
        assert set(item) == {"id", "actor_id", "target_id", "created_at", "action", "reason",
                             "before_version", "after_version"}
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.skipif(not POSTGRES_URL, reason="requires isolated PostgreSQL for concurrent policy commit")
def test_audit_snapshot_survives_policy_commit_after_statement(tmp_path):
    async def scenario():
        url = database_url(tmp_path / "audit-snapshot.db")
        database = Database(url)
        await database.migrate()
        try:
            actor_id, target_id, _, _ = await seed_audits(database, count=0)
            async with database.session() as session:
                await policy_transaction(session, lambda: change_ai_access(session,
                    actor_id=actor_id, target_id=target_id, frozen=True, reason="before snapshot",
                    expected_version=0, idempotency_key="first-freeze"))
            writer_errors = []
            inserted = False

            def commit_after_snapshot(connection, cursor, statement, parameters, context, executemany):
                nonlocal inserted
                if (inserted or "ai_policy_audits" not in statement
                        or not statement.lstrip().upper().startswith(("SELECT", "WITH"))):
                    return
                inserted = True

                async def write():
                    other = Database(url)
                    try:
                        async with other.session() as session:
                            await policy_transaction(session, lambda: change_ai_access(session,
                                actor_id=actor_id, target_id=target_id, frozen=False,
                                reason="after snapshot", expected_version=1,
                                idempotency_key="concurrent-unfreeze"))
                    finally:
                        await other.dispose()

                def target():
                    try:
                        asyncio.run(write())
                    except BaseException as error:
                        writer_errors.append(error)

                thread = threading.Thread(target=target)
                thread.start()
                thread.join(timeout=10)
                assert not thread.is_alive()

            event.listen(database.engine.sync_engine, "after_cursor_execute", commit_after_snapshot)
            try:
                async with database.session() as session:
                    response = await policy_audit(target_id, None, session, page=1, page_size=25)
            finally:
                event.remove(database.engine.sync_engine, "after_cursor_execute", commit_after_snapshot)
            assert inserted and not writer_errors
            body = json.loads(response.body)
            assert body["total"] == len(body["items"]) == 1
            assert body["items"][0]["action"] == "freeze"
            async with database.session() as session:
                final = json.loads((await policy_audit(target_id, None, session, page=1, page_size=25)).body)
            assert final["total"] == len(final["items"]) == 2
            assert final["items"][0]["action"] == "unfreeze"
        finally:
            await database.dispose()
    asyncio.run(scenario())
