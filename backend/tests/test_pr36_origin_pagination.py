"""Public-origin, snapshot pagination and UTC regressions for PR36."""
import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event

from backend import admin_queries
from backend.config import Settings
from backend.db import Database
from backend.tests.databases import POSTGRES_URL, database_url
from backend.api.admin import policy_users
from backend.models import User
from backend.tests.auth_helpers import sign_in
from backend.tests.test_history_api import make_app


def test_configured_https_origin_ignores_untrusted_forwarded_headers(tmp_path):
    app = make_app(tmp_path)
    app.state.settings.admin_policy_management_enabled = True
    app.state.settings.admin_public_origin = "https://admin.example.test"
    with TestClient(app, base_url="http://admin.example.test") as client:
        target = sign_in(client, "origin-target@example.test")
        client.cookies.clear()
        operator = sign_in(client, "origin-operator@example.test")

        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, operator["id"])
                user.role = "admin"
                await session.commit()

        client.portal.call(grant)
        path = f"/api/admin/v1/users/{target['id']}/ai-access"
        headers = {"X-CoreTeams-Admin": "1", "Idempotency-Key": "https-origin"}
        body = {"frozen": True, "expected_version": 0, "reason": "synthetic"}
        for origin in ("https://evil.test", "null", "https://admin.example.test:444", "http://admin.example.test"):
            assert client.patch(path, json=body, headers={**headers,
                "Host": "evil.test", "Origin": origin, "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": origin}).status_code == 403
        assert client.patch(path, json=body, headers={**headers,
            "Origin": "https://admin.example.test", "X-Forwarded-Proto": "http"}).status_code == 200


def test_users_page_uses_one_snapshot_including_empty_page(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        sign_in(client, "page@example.test")

        async def scenario():
            statements = []
            def capture(connection, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    statements.append(statement)
            engine = app.state.database.engine.sync_engine
            event.listen(engine, "before_cursor_execute", capture)
            try:
                async with app.state.database.session() as session:
                    response = await policy_users(None, session, q="page@", page=1, page_size=25)
                    assert len(statements) == 1
                    body = json.loads(response.body)
                    assert body["total"] == len(body["items"]) == 1
                    statements.clear()
                    response = await policy_users(None, session, q="page@", page=9, page_size=25)
                    body = json.loads(response.body)
                    assert body["total"] == 1 and body["items"] == []
                    assert len(statements) == 1
            finally:
                event.remove(engine, "before_cursor_execute", capture)
        client.portal.call(scenario)


def test_operational_stamp_normalizes_aware_offset():
    stamp = datetime(2026, 11, 12, 15, tzinfo=timezone(timedelta(hours=3)))
    assert admin_queries._stamp(stamp) == "2026-11-12T12:00:00+00:00"
    assert admin_queries._stamp(stamp.replace(tzinfo=None)) == "2026-11-12T15:00:00+00:00"
    assert admin_queries._stamp(None) is None


@pytest.mark.parametrize("origin", ["https://example.test/path", "https://user:pass@example.test",
    "null", "https://example.test?x=1", "https://example.test/#x", "https://example.test:99999",
    "https://example.test:", "https://EXAMPLE.test", "https://example.test:443",
    "https://example.test:0443", "http://example.test:80", "https://example.test:0123",
    "https://exa mple.test", "https://example.test\u0000", "https://example.test\u007f",
    "https://пример.test", "https://[0:0:0:0:0:0:0:1]", "https://127.1",
    "https://0x7f000001", "https://0x7f.0x0.0x0.0x1", "https://0177.0.0.1",
    "https://example.123", "https://example.0xff", "https://[fe80::1%25eth0]"])
def test_invalid_configured_origin_rejected(origin):
    with pytest.raises(ValidationError):
        Settings(admin_public_origin=origin)


def test_public_origin_unset_does_not_trust_forwarding(tmp_path):
    app = make_app(tmp_path)
    app.state.settings.admin_policy_management_enabled = True
    with TestClient(app, base_url="http://internal.test") as client:
        operator = sign_in(client, "no-trusted-proxy@example.test")
        async def grant():
            async with app.state.database.session() as session:
                user = await session.get(User, operator["id"])
                user.role = "admin"
                await session.commit()
        client.portal.call(grant)
        response = client.patch("/api/admin/v1/users/missing/ai-access", json={}, headers={
            "Origin": "https://internal.test", "X-Forwarded-Proto": "https",
            "Forwarded": "proto=https;host=internal.test", "X-CoreTeams-Admin": "1"})
        assert response.status_code == 403
        assert response.json()["code"] == "invalid_admin_origin"


@pytest.mark.skipif(not POSTGRES_URL, reason="requires isolated PostgreSQL for concurrent commit")
def test_users_snapshot_survives_registration_after_statement(tmp_path):
    async def scenario():
        url = database_url(tmp_path / "snapshot.db")
        database = Database(url)
        await database.migrate()
        try:
            async with database.session() as session:
                session.add(User(email="snapshot-first@example.test", password_hash="synthetic"))
                await session.commit()
            writer_errors = []
            inserted = False
            def commit_after_snapshot(connection, cursor, statement, parameters, context, executemany):
                nonlocal inserted
                if inserted or not statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    return
                inserted = True
                async def write():
                    other = Database(url)
                    try:
                        async with other.session() as session:
                            session.add(User(email="snapshot-new@example.test", password_hash="synthetic"))
                            await session.commit()
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
                    response = await policy_users(None, session, q="snapshot-", page=1, page_size=25)
            finally:
                event.remove(database.engine.sync_engine, "after_cursor_execute", commit_after_snapshot)
            assert not writer_errors
            body = json.loads(response.body)
            assert body["total"] == len(body["items"]) == 1
            assert body["items"][0]["email"] == "snapshot-first@example.test"
            async with database.session() as session:
                final = json.loads((await policy_users(None, session, q="snapshot-", page=1, page_size=25)).body)
                assert final["total"] == len(final["items"]) == 2
        finally:
            await database.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["https://admin.example.test", "http://localhost:5173",
    "https://[::1]", "http://127.0.0.1:5173", "https://admin.example.test:8443"])
def test_canonical_public_origins_are_preserved(origin):
    assert Settings(admin_public_origin=origin).admin_public_origin == origin
