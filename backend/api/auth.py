import asyncio

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.api.history import SessionDependency, SettingsDependency
from backend.auth import (
    SESSION_COOKIE,
    CurrentUser,
    check_credentials,
    create_session,
    hash_password,
    revoke_session,
)
from backend.config import Settings
from backend.models import User
from backend.schemas import Credentials, UserRead


router = APIRouter(prefix="/api/auth", tags=["auth"])


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(
    credentials: Credentials,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> UserRead:
    if await session.scalar(select(User.id).where(User.email == credentials.email)):
        raise HTTPException(status_code=409, detail="Email already registered")

    password_hash = await asyncio.to_thread(hash_password, credentials.password)
    user = User(email=credentials.email, password_hash=password_hash)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Параллельная регистрация того же email.
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None

    token = await create_session(session, user, settings.auth_session_ttl_seconds)
    set_session_cookie(response, token, settings)
    return UserRead.model_validate(user)


@router.post("/login", response_model=UserRead)
async def login(
    credentials: Credentials,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> UserRead:
    user = await check_credentials(session, credentials.email, credentials.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = await create_session(session, user, settings.auth_session_ttl_seconds)
    set_session_cookie(response, token, settings)
    return UserRead.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, session: SessionDependency) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await revoke_session(session, token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
