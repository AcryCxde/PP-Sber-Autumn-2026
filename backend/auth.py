"""Пароли, серверные сессии и определение текущего пользователя.

Сессия — случайный токен в HttpOnly-cookie; в базе лежит только его SHA-256.
Чужие и несуществующие ресурсы API отдаёт одинаково (404), чтобы не раскрывать их наличие.
"""

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies import get_session
from backend.models import AuthSession, Conversation, Run, User


SESSION_COOKIE = "coreteams_session"

# scrypt из стандартной библиотеки: ~16 МБ памяти и десятки миллисекунд на попытку.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=64 * 1024 * 1024,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected_bytes = base64.b64decode(expected)
        actual = _scrypt(
            password,
            base64.b64decode(salt),
            int(n),
            int(r),
            int(p),
            len(expected_bytes),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected_bytes)


# Для неизвестного email всё равно считаем scrypt: время ответа не выдаёт, есть ли аккаунт.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


async def check_credentials(session: AsyncSession, email: str, password: str) -> User | None:
    user = await session.scalar(select(User).where(User.email == email))
    encoded = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    valid = await asyncio.to_thread(verify_password, password, encoded)
    return user if user is not None and valid else None


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(session: AsyncSession, user: User, ttl_seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(
        AuthSession(
            id=_token_id(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    await session.commit()
    return token


async def revoke_session(session: AsyncSession, token: str) -> None:
    await session.execute(delete(AuthSession).where(AuthSession.id == _token_id(token)))
    await session.commit()


async def user_for_token(session: AsyncSession, token: str | None) -> User | None:
    if not token:
        return None
    auth_session = await session.get(AuthSession, _token_id(token))
    if auth_session is None:
        return None
    expires_at = auth_session.expires_at
    # SQLite возвращает время без зоны, хотя пишем мы его в UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        await session.delete(auth_session)
        await session.commit()
        return None
    return await session.get(User, auth_session.user_id)


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    user = await user_for_token(session, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def websocket_user_id(websocket: WebSocket) -> str | None:
    async with websocket.app.state.database.session() as session:
        user = await user_for_token(session, websocket.cookies.get(SESSION_COOKIE))
    return user.id if user is not None else None


async def owns_conversation(session: AsyncSession, conversation_id: str, owner_id: str) -> bool:
    found = await session.scalar(
        select(Conversation.id).where(
            Conversation.id == conversation_id,
            Conversation.owner_id == owner_id,
        )
    )
    return found is not None


async def owns_run(session: AsyncSession, run_id: str, owner_id: str) -> bool:
    found = await session.scalar(
        select(Run.id)
        .join(Conversation, Conversation.id == Run.conversation_id)
        .where(Run.id == run_id, Conversation.owner_id == owner_id)
    )
    return found is not None
