"""Private AI policy and transaction-scoped execution admission.

Services flush only: callers own commit/rollback, and may publish events only
following a successful commit. SQL checks are structural; Unicode whitespace
normalization is enforced here before both hashing and audit persistence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AiPolicyAudit, Conversation, Event, Run, User
from backend.repository import (
    TERMINAL_RUN_STATUSES, _append_event_in_transaction, _lock_run_context,
    apply_run_context_reset, finalize_run,
)
from backend.public_events import run_started_event
from backend.timestamps import utc_iso


class AiAccessError(Exception):
    def __init__(self, code: str, status_code: int, message: str,
                 current: dict[str, bool | int] | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.current = current


@dataclass(frozen=True)
class PolicyMutationResult:
    response: dict[str, object]


class AdmissionState(StrEnum):
    ADMITTED = "admitted"
    ALREADY_ADMITTED = "already_admitted"
    TERMINAL = "terminal"
    DENIED = "denied"


@dataclass(frozen=True)
class AdmissionResult:
    state: AdmissionState
    event: Event | None = None
    run_id: str | None = None
    context_events: tuple[Event, ...] = ()


def normalize_policy_request(*, frozen: bool, reason: str | None,
                             expected_version: int, idempotency_key: str) -> str | None:
    if type(frozen) is not bool or type(expected_version) is not int or expected_version < 0:
        raise AiAccessError("invalid_policy_request", 422, "Некорректная политика AI-доступа.")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 64 or not idempotency_key.strip():
        raise AiAccessError("invalid_idempotency_key", 422, "Некорректный ключ подтверждения.")
    if reason is not None and not isinstance(reason, str):
        raise AiAccessError("invalid_policy_request", 422, "Некорректная причина изменения.")
    normalized = reason.strip() if reason is not None else None
    if (frozen and not normalized) or (normalized is not None and (len(normalized) > 1000 or "\x00" in normalized)):
        raise AiAccessError("invalid_policy_request", 422, "Укажите причину длиной от 1 до 1000 символов.")
    return normalized


async def _lock_user(session: AsyncSession, user_id: str) -> User:
    # A write lock works on SQLite as well as PostgreSQL; never use stale ORM
    # values read before waiting for another policy/admission transaction.
    user = await session.scalar(
        update(User).where(User.id == user_id)
        .values(ai_policy_version=User.ai_policy_version)
        .returning(User).execution_options(populate_existing=True)
    )
    if user is None:
        raise AiAccessError("user_not_found", 404, "Пользователь не найден.")
    return user


async def change_ai_access(session: AsyncSession, *, actor_id: str, target_id: str,
                           frozen: bool, reason: str | None, expected_version: int,
                           idempotency_key: str) -> PolicyMutationResult:
    normalized = normalize_policy_request(frozen=frozen, reason=reason,
        expected_version=expected_version, idempotency_key=idempotency_key)
    actor_role = await session.scalar(select(User.role).where(User.id == actor_id))
    if actor_role != "admin":
        raise AiAccessError("admin_required", 403, "Требуются права администратора.")
    if frozen and actor_id == target_id:
        raise AiAccessError("self_freeze_forbidden", 403, "Нельзя заморозить собственный AI-доступ.")
    body = json.dumps({"actor_id": actor_id, "target_id": target_id, "frozen": frozen,
                       "reason": normalized, "expected_version": expected_version},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(body.encode()).hexdigest()
    user = await _lock_user(session, target_id)
    previous = await session.scalar(select(AiPolicyAudit).where(
        AiPolicyAudit.actor_id == actor_id, AiPolicyAudit.idempotency_key == idempotency_key))
    if previous is not None:
        if previous.target_id != target_id or previous.request_hash != request_hash:
            raise AiAccessError("idempotency_conflict", 409, "Ключ уже использован для другого изменения.")
        return PolicyMutationResult(previous.response)
    if user.ai_policy_version != expected_version:
        raise AiAccessError("policy_version_conflict", 409, "AI-доступ уже изменён.",
                            {"frozen": user.ai_frozen, "version": user.ai_policy_version})
    if user.ai_frozen == frozen:
        raise AiAccessError("policy_state_conflict", 409, "AI-доступ уже находится в выбранном состоянии.")
    before = user.ai_policy_version
    user.ai_frozen = frozen
    user.ai_policy_version += 1
    now = datetime.now(UTC)
    audit_id = str(uuid4())
    response: dict[str, object] = {
        "user": {"id": user.id, "email": user.email,
                 "created_at": utc_iso(user.created_at),
                 "last_seen_at": utc_iso(user.last_seen_at) if user.last_seen_at else None,
                 "ai_access": {"frozen": user.ai_frozen, "version": user.ai_policy_version}},
        "audit_id": audit_id, "changed_at": now.isoformat(),
    }
    session.add(AiPolicyAudit(id=audit_id, actor_id=actor_id, target_id=target_id,
        created_at=now, action="freeze" if frozen else "unfreeze", reason=normalized,
        before_version=before, after_version=user.ai_policy_version,
        idempotency_key=idempotency_key, request_hash=request_hash, response=response))
    await session.flush()
    return PolicyMutationResult(response)


async def admit_run(session: AsyncSession, run_id: str) -> AdmissionResult:
    context = (await session.execute(select(Run.conversation_id, Conversation.owner_id)
        .join(Conversation, Run.conversation_id == Conversation.id).where(Run.id == run_id))).one_or_none()
    if context is None:
        raise AiAccessError("run_not_found", 404, "Запуск не найден.")
    owner_id = context.owner_id
    if owner_id is None:
        raise AiAccessError("ai_policy_unavailable", 503, "Невозможно проверить AI-доступ.")
    try:
        user = await _lock_user(session, owner_id)
    except AiAccessError as error:
        raise AiAccessError("ai_policy_unavailable", 503, "Невозможно проверить AI-доступ.") from error
    locked = await _lock_run_context(session, run_id)
    if locked is None:
        raise AiAccessError("run_not_found", 404, "Запуск не найден.")
    run, conversation = locked
    if conversation.owner_id != user.id:
        raise AiAccessError("ai_policy_unavailable", 503, "Владелец запуска изменён.")
    if run.status in TERMINAL_RUN_STATUSES:
        return AdmissionResult(AdmissionState.TERMINAL)
    if run.admitted_at is not None:
        return AdmissionResult(AdmissionState.ALREADY_ADMITTED)
    if run.status != "pending":
        raise AiAccessError("ai_policy_unavailable", 503, "Состояние запуска требует восстановления.")
    if user.ai_frozen:
        result = await finalize_run(session, run.id, assistant_content=None, status="failed",
                                    exit_code=None, error_code="ai_access_frozen", commit=False)
        return AdmissionResult(AdmissionState.DENIED, result.terminal_event)
    reset_event = await apply_run_context_reset(session, run, conversation)
    now = datetime.now(UTC)
    run.admitted_at = now
    run.admission_policy_version = user.ai_policy_version
    run.status = "running"
    run.started_at = now
    event = await _append_event_in_transaction(session, run.id, run_started_event())
    await session.flush()
    return AdmissionResult(AdmissionState.ADMITTED, event, run.id,
                           (reset_event,) if reset_event is not None else ())


T = TypeVar("T")


def _retryable(error: DBAPIError) -> bool:
    original = error.orig
    sqlstate = getattr(original, "sqlstate", None)
    sqlite_code = getattr(original, "sqlite_errorcode", None)
    # Unique violation may be concurrent actor/key receipts on different targets.
    return sqlstate in {"40P01", "23505", "55P03", "57014"} or (sqlite_code is not None and sqlite_code & 0xff in {5, 6})


async def policy_transaction(session: AsyncSession, operation: Callable[[], Awaitable[T]]) -> T:
    """Own the transaction, retry only classified concurrency failures, bounded."""
    for attempt in range(3):
        try:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(text("SET LOCAL lock_timeout = '2s'"))
                await session.execute(text("SET LOCAL statement_timeout = '5s'"))
            value = await operation()
            await session.commit()
            return value
        except DBAPIError as error:
            await session.rollback()
            if not _retryable(error) or attempt == 2:
                raise AiAccessError("ai_policy_unavailable", 503, "AI-доступ временно недоступен.") from error
            await asyncio.sleep(0.01 * (attempt + 1))
        except AiAccessError:
            await session.rollback()
            raise
    raise AssertionError("bounded retry exhausted")
