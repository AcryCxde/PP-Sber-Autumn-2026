from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from starlette.websockets import WebSocketDisconnect

from backend.auth import SESSION_COOKIE, hash_password, verify_password
from backend.models import AuthSession, User
from backend.tests.auth_helpers import PASSWORD, sign_in
from backend.tests.test_history_api import make_app


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first.startswith("scrypt$")
    assert PASSWORD not in first
    assert first != second
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong password", first)
    assert not verify_password(PASSWORD, "not-a-hash")


def test_register_sets_session_cookie_and_me_returns_user(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register",
            json={"email": "  Alpha@Example.COM ", "password": PASSWORD},
        )
        me = client.get("/api/auth/me")

        async def stored():
            async with app.state.database.session() as session:
                user = await session.scalar(select(User))
                token_ids = list(await session.scalars(select(AuthSession.id)))
            return user, token_ids

        user, token_ids = client.portal.call(stored)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alpha@example.com"
    assert set(body) == {"id", "email", "role", "created_at"}
    assert body["role"] == "user"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert me.status_code == 200 and me.json()["id"] == body["id"]
    # В базе только хеш пароля и хеш токена.
    assert user.password_hash != PASSWORD
    token = client.cookies.get(SESSION_COOKIE)
    assert token is None or token not in token_ids
    assert len(token_ids) == 1 and len(token_ids[0]) == 64


def test_duplicate_email_is_rejected_case_insensitively(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client, "alpha@example.com")
        response = client.post(
            "/api/auth/register",
            json={"email": "ALPHA@example.com", "password": PASSWORD},
        )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "alpha@example.com", "password": "short"},
        {"email": "alpha@example.com"},
    ],
)
def test_register_validates_input(tmp_path, payload) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        response = client.post("/api/auth/register", json=payload)

    assert response.status_code == 422
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_login_rejects_wrong_password_and_unknown_email_identically(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        client.cookies.clear()
        wrong = client.post(
            "/api/auth/login",
            json={"email": "alpha@example.com", "password": "wrong password"},
        )
        unknown = client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": PASSWORD},
        )
        me = client.get("/api/auth/me")
        ok = client.post(
            "/api/auth/login",
            json={"email": "Alpha@Example.com", "password": PASSWORD},
        )
        me_after = client.get("/api/auth/me")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert me.status_code == 401
    assert ok.status_code == 200
    assert me_after.status_code == 200


def test_logout_revokes_session_server_side(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)
        token = client.cookies.get(SESSION_COOKIE)
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/me").status_code == 401
        # Старый токен, сохранённый атакующим, тоже больше не действует.
        client.cookies.set(SESSION_COOKIE, token)
        replay = client.get("/api/auth/me")

    assert replay.status_code == 401


def test_expired_session_is_rejected(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        sign_in(client)

        async def expire():
            async with app.state.database.session() as session:
                await session.execute(
                    update(AuthSession).values(
                        expires_at=datetime.now(UTC) - timedelta(seconds=1)
                    )
                )
                await session.commit()

        client.portal.call(expire)
        response = client.get("/api/auth/me")

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/conversations"),
        ("GET", "/api/conversations/any/messages"),
        ("GET", "/api/conversations/any/events"),
        ("DELETE", "/api/conversations/any"),
        ("GET", "/api/runs/any/events"),
        ("GET", "/api/runs/any/events/1/file"),
        ("GET", "/api/auth/me"),
    ],
)
def test_history_requires_authentication(tmp_path, method, path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        response = client.request(method, path)

    assert response.status_code == 401


def test_websocket_without_session_is_closed(tmp_path) -> None:
    app = make_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()

    assert closed.value.code == 4401
