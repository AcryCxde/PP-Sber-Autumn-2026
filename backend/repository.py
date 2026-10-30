from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Artifact, Conversation, Event, Message, Project, Run
from backend.public_events import (
    SAFE_FAILURE_CODES,
    SAFE_UNKNOWN_CODES,
    TERMINAL_EVENT_TYPES,
    artifact_saved_event,
    context_reset_event,
    run_accepted_event,
    run_started_event,
    validate_public_event_data,
)


ACTIVE_RUN_STATUSES = {"pending", "running"}
TERMINAL_RUN_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "outcome_unknown",
}


class RunAcceptanceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreatedRequest:
    conversation_id: str
    run_id: str
    message_id: str


@dataclass(frozen=True)
class RunAcceptance:
    conversation_id: str
    run_id: str
    client_request_id: str
    context_mode: str
    resume_session_id: str | None
    user_message: Message
    accepted_event: Event
    reset_event: Event | None
    idempotent_replay: bool


@dataclass(frozen=True)
class RunFinalization:
    assistant_message: Message | None
    terminal_event: Event
    context_state: str
    status: str


@dataclass(frozen=True)
class RunEventPage:
    conversation_id: str
    run_status: str
    events: list[Event]


# Заголовок пустого диалога, созданного ради вложений; первый запрос заменяет его.
NEW_CONVERSATION_TITLE = "Новый диалог"


def make_title(prompt: str, max_length: int = 80) -> str:
    title = " ".join(prompt.split())
    if len(title) <= max_length:
        return title
    return f"{title[: max_length - 1].rstrip()}…"


def message_payload(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "run_id": message.run_id,
        "role": message.role,
        "kind": message.kind,
        "sequence": message.sequence,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }


async def _next_message_sequence(
    session: AsyncSession,
    conversation_id: str,
) -> int:
    sequence = await session.scalar(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(next_message_seq=Conversation.next_message_seq + 1)
        .returning(Conversation.next_message_seq)
    )
    if sequence is None:
        raise LookupError(f"Conversation {conversation_id} was not found")
    return sequence


async def _append_event_in_transaction(
    session: AsyncSession,
    run_id: str,
    event_data: Mapping[str, Any],
    *,
    allow_terminal: bool = False,
) -> Event:
    safe_event = validate_public_event_data(event_data)
    statement = update(Run).where(Run.id == run_id)
    if not allow_terminal:
        statement = statement.where(Run.status.not_in(TERMINAL_RUN_STATUSES))

    next_seq = await session.scalar(
        statement
        .values(next_event_seq=Run.next_event_seq + 1)
        .returning(Run.next_event_seq)
    )
    if next_seq is None:
        run_status = await session.scalar(select(Run.status).where(Run.id == run_id))
        if run_status is None:
            raise LookupError(f"Run {run_id} was not found")
        raise ValueError(f"Run {run_id} is already terminal")

    event_type = str(safe_event["type"])
    payload = {key: value for key, value in safe_event.items() if key != "type"}
    event = Event(
        run_id=run_id,
        seq=next_seq,
        type=event_type,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def _existing_acceptance(
    session: AsyncSession,
    run: Run,
    *,
    requested_conversation_id: str | None,
    requested_content: str,
    requested_context_mode: str,
    owner_id: str | None = None,
) -> RunAcceptance:
    if owner_id is not None:
        conversation = await session.get(Conversation, run.conversation_id)
        if conversation is None or conversation.owner_id != owner_id:
            # Тот же ответ, что и при несовпадении запроса: чужой run не раскрывается.
            raise RunAcceptanceError(
                "idempotency_conflict",
                "client_request_id уже использован для другого запроса.",
            )
    user_message = await session.scalar(
        select(Message)
        .where(Message.run_id == run.id, Message.role == "user")
        .order_by(Message.sequence.asc())
    )
    accepted_event = await session.scalar(
        select(Event)
        .where(Event.run_id == run.id, Event.type == "run.accepted")
        .order_by(Event.seq.asc())
    )
    reset_event = await session.scalar(
        select(Event)
        .where(Event.run_id == run.id, Event.type == "context.reset")
        .order_by(Event.seq.asc())
    )
    if user_message is None or accepted_event is None:
        raise RunAcceptanceError(
            "idempotency_state_invalid",
            "Существующий запрос не содержит canonical acknowledgement.",
        )

    conversation_matches = (
        requested_conversation_id is None
        or requested_conversation_id == run.conversation_id
    )
    mode_matches = (
        requested_context_mode == "resume"
        and run.context_mode in {"new", "resume"}
    ) or requested_context_mode == run.context_mode
    if (
        not conversation_matches
        or user_message.content != requested_content
        or not mode_matches
    ):
        raise RunAcceptanceError(
            "idempotency_conflict",
            "client_request_id уже использован для другого запроса.",
        )

    return RunAcceptance(
        conversation_id=run.conversation_id,
        run_id=run.id,
        client_request_id=run.client_request_id,
        context_mode=run.context_mode,
        resume_session_id=run.resume_session_id,
        user_message=user_message,
        accepted_event=accepted_event,
        reset_event=reset_event,
        idempotent_replay=True,
    )


async def _accept_run_once(
    session: AsyncSession,
    *,
    conversation_id: str | None,
    client_request_id: str,
    content: str,
    requested_context_mode: str = "resume",
    owner_id: str | None = None,
) -> RunAcceptance:
    existing_run = await session.scalar(
        select(Run).where(Run.client_request_id == client_request_id)
    )
    if existing_run is not None:
        return await _existing_acceptance(
            session,
            existing_run,
            requested_conversation_id=conversation_id,
            requested_content=content,
            requested_context_mode=requested_context_mode,
            owner_id=owner_id,
        )

    if requested_context_mode not in {"resume", "reset"}:
        raise RunAcceptanceError("invalid_context_mode", "Некорректный context_mode.")

    reset_message: Message | None = None
    if conversation_id is None:
        if requested_context_mode == "reset":
            raise RunAcceptanceError(
                "invalid_context_mode",
                "Нельзя сбросить контекст нового диалога.",
            )
        conversation = Conversation(
            owner_id=owner_id,
            title=make_title(content),
            context_state="new",
            next_message_seq=0,
        )
        session.add(conversation)
        await session.flush()
    else:
        conversation = await session.get(Conversation, conversation_id)
        if (
            conversation is None
            or conversation.status == "deleted"
            or (owner_id is not None and conversation.owner_id != owner_id)
        ):
            raise RunAcceptanceError(
                "conversation_not_found",
                "Диалог не найден.",
            )

        active_run = await session.scalar(
            select(Run).where(
                Run.conversation_id == conversation.id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        if active_run is not None:
            raise RunAcceptanceError(
                "conversation_busy",
                "В этом диалоге уже выполняется запрос.",
            )

        if requested_context_mode == "resume" and conversation.context_state == "unavailable":
            raise RunAcceptanceError(
                "conversation_context_unavailable",
                "Сохранённый контекст Claude недоступен. Выполните явный сброс.",
            )
        if conversation.next_message_seq == 0 and conversation.title == NEW_CONVERSATION_TITLE:
            conversation.title = make_title(content)

    previous_session_id = conversation.claude_session_id
    if requested_context_mode == "reset":
        context_mode = "reset"
        conversation.claude_session_id = None
        conversation.context_state = "reset"
    elif conversation.claude_session_id:
        context_mode = "resume"
    else:
        context_mode = "new"

    run = Run(
        conversation=conversation,
        status="pending",
        client_request_id=client_request_id,
        context_mode=context_mode,
        resume_session_id=previous_session_id if context_mode in {"resume", "reset"} else None,
    )
    session.add(run)
    await session.flush()

    reset_event: Event | None = None
    if context_mode == "reset":
        reset_message = Message(
            conversation_id=conversation.id,
            run_id=run.id,
            role="system",
            kind="context_reset",
            sequence=await _next_message_sequence(session, conversation.id),
            content="Контекст Claude был явно сброшен.",
        )
        session.add(reset_message)
        await session.flush()
        reset_event = await _append_event_in_transaction(
            session,
            run.id,
            context_reset_event(message_payload(reset_message)),
        )

    user_message = Message(
        conversation_id=conversation.id,
        run_id=run.id,
        role="user",
        kind="text",
        sequence=await _next_message_sequence(session, conversation.id),
        content=content,
    )
    session.add(user_message)
    await session.flush()

    accepted_event = await _append_event_in_transaction(
        session,
        run.id,
        run_accepted_event(
            client_request_id=client_request_id,
            context_mode=context_mode,
            message=message_payload(user_message),
        ),
    )
    conversation.updated_at = datetime.now(UTC)
    await session.commit()

    return RunAcceptance(
        conversation_id=conversation.id,
        run_id=run.id,
        client_request_id=client_request_id,
        context_mode=context_mode,
        resume_session_id=run.resume_session_id,
        user_message=user_message,
        accepted_event=accepted_event,
        reset_event=reset_event,
        idempotent_replay=False,
    )


async def client_request_exists(
    session: AsyncSession,
    client_request_id: str,
) -> bool:
    run_id = await session.scalar(
        select(Run.id).where(Run.client_request_id == client_request_id)
    )
    return run_id is not None


async def accept_run(
    session: AsyncSession,
    *,
    conversation_id: str | None,
    client_request_id: str,
    content: str,
    requested_context_mode: str = "resume",
    owner_id: str | None = None,
) -> RunAcceptance:
    """owner_id: владелец нового диалога; существующий диалог и повтор запроса должны быть его."""
    try:
        return await _accept_run_once(
            session,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            content=content,
            requested_context_mode=requested_context_mode,
            owner_id=owner_id,
        )
    except IntegrityError:
        await session.rollback()

        existing_run = await session.scalar(
            select(Run).where(Run.client_request_id == client_request_id)
        )
        if existing_run is not None:
            return await _existing_acceptance(
                session,
                existing_run,
                requested_conversation_id=conversation_id,
                requested_content=content,
                requested_context_mode=requested_context_mode,
                owner_id=owner_id,
            )

        if conversation_id is not None:
            active_run = await session.scalar(
                select(Run).where(
                    Run.conversation_id == conversation_id,
                    Run.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
            if active_run is not None:
                raise RunAcceptanceError(
                    "conversation_busy",
                    "В этом диалоге уже выполняется запрос.",
                )
        raise


async def create_request(
    session: AsyncSession,
    prompt: str,
    *,
    owner_id: str | None = None,
) -> CreatedRequest:
    accepted = await accept_run(
        session,
        conversation_id=None,
        client_request_id=str(uuid4()),
        content=prompt,
        owner_id=owner_id,
    )
    return CreatedRequest(
        conversation_id=accepted.conversation_id,
        run_id=accepted.run_id,
        message_id=accepted.user_message.id,
    )


async def mark_run_running(session: AsyncSession, run_id: str) -> Event | None:
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"Run {run_id} was not found")
    if run.status != "pending":
        return None

    run.status = "running"
    run.started_at = datetime.now(UTC)
    event = await _append_event_in_transaction(
        session,
        run_id,
        run_started_event(),
    )
    await session.commit()
    return event


def _terminal_event_data(
    run: Run,
    conversation: Conversation,
    assistant_message: Message | None,
) -> dict[str, Any]:
    if run.status == "completed":
        return {
            "type": "run.completed",
            "schema_version": 1,
            "status": "completed",
            "client_request_id": run.client_request_id,
            "session_state": conversation.context_state,
            "assistant_message": (
                message_payload(assistant_message) if assistant_message else None
            ),
        }

    if run.status == "outcome_unknown":
        code = (
            run.error_code
            if run.error_code in SAFE_UNKNOWN_CODES
            else "heartbeat_lost"
        )
        return {
            "type": "run.outcome_unknown",
            "schema_version": 1,
            "status": "outcome_unknown",
            "error_code": code,
            "message": "Не удалось определить результат запуска.",
            "session_state": conversation.context_state,
            "retryable": False,
            "can_reset_context": True,
        }

    code = (
        run.error_code
        if run.error_code in SAFE_FAILURE_CODES
        else "claude_request_failed"
    )
    return {
        "type": "run.failed",
        "schema_version": 1,
        "status": run.status,
        "error_code": code,
        "message": "Запрос Claude завершился с ошибкой.",
        "session_state": conversation.context_state,
        "retryable": code in {"claude_cli_failed", "result_error"},
        "can_reset_context": conversation.context_state == "unavailable",
    }


async def finalize_run(
    session: AsyncSession,
    run_id: str,
    *,
    assistant_content: str | None,
    status: str,
    exit_code: int | None,
    session_id: str | None = None,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
    turns: int | None = None,
    error: str | None = None,
    error_code: str | None = None,
) -> RunFinalization:
    del error
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"Unsupported terminal run status: {status}")

    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"Run {run_id} was not found")

    conversation = await session.get(Conversation, run.conversation_id)
    if conversation is None:
        raise LookupError(f"Conversation {run.conversation_id} was not found")

    assistant_message = await session.scalar(
        select(Message).where(Message.run_id == run.id, Message.role == "assistant")
    )
    if run.status in TERMINAL_RUN_STATUSES:
        terminal_event = await session.scalar(
            select(Event)
            .where(Event.run_id == run.id, Event.type.in_(TERMINAL_EVENT_TYPES))
            .order_by(Event.seq.desc())
        )
        if terminal_event is None:
            terminal_event = await _append_event_in_transaction(
                session,
                run.id,
                _terminal_event_data(run, conversation, assistant_message),
                allow_terminal=True,
            )
            await session.commit()
        return RunFinalization(
            assistant_message=assistant_message,
            terminal_event=terminal_event,
            context_state=conversation.context_state,
            status=run.status,
        )

    if status == "completed" and run.context_mode == "resume":
        if session_id != run.resume_session_id:
            status = "failed"
            error_code = "claude_session_mismatch"

    if assistant_content:
        assistant_message = Message(
            conversation_id=conversation.id,
            run_id=run.id,
            role="assistant",
            kind="text",
            sequence=await _next_message_sequence(session, conversation.id),
            content=assistant_content,
        )
        session.add(assistant_message)
        await session.flush()

    now = datetime.now(UTC)
    run.status = status
    run.completed_at = now
    run.exit_code = exit_code
    run.duration_ms = duration_ms
    run.cost_usd = Decimal(str(cost_usd)) if cost_usd is not None else None
    run.turns = turns
    run.error = None
    run.error_code = error_code
    run.claude_session_id = session_id
    conversation.updated_at = now

    if status == "completed" and session_id:
        conversation.claude_session_id = session_id
        conversation.context_state = "active"
    elif status == "completed":
        conversation.claude_session_id = None
        conversation.context_state = "unavailable"
    elif status == "outcome_unknown" or error_code in {
        "artifact_save_failed",
        "claude_cli_failed",
        "claude_request_failed",
        "claude_session_unavailable",
        "claude_session_mismatch",
        "claude_session_state_uncertain",
    }:
        conversation.claude_session_id = None
        conversation.context_state = "unavailable"

    terminal_event = await _append_event_in_transaction(
        session,
        run.id,
        _terminal_event_data(run, conversation, assistant_message),
        allow_terminal=True,
    )
    await session.commit()
    return RunFinalization(
        assistant_message=assistant_message,
        terminal_event=terminal_event,
        context_state=conversation.context_state,
        status=run.status,
    )


async def append_event(
    session: AsyncSession,
    run_id: str,
    event_data: Mapping[str, Any],
) -> Event:
    event = await _append_event_in_transaction(session, run_id, event_data)
    await session.commit()
    return event


async def append_artifact(
    session: AsyncSession,
    run_id: str,
    *,
    path: str,
    content: str | None,
    size_bytes: int | None,
    truncated: bool,
    num_lines: int | None,
) -> Event:
    event = await _append_event_in_transaction(
        session,
        run_id,
        artifact_saved_event(
            path=path,
            size_bytes=size_bytes,
            truncated=truncated,
            num_lines=num_lines,
        ),
    )
    session.add(
        Artifact(
            run_id=run_id,
            event_seq=event.seq,
            path=path,
            content=content,
            size_bytes=size_bytes,
            truncated=truncated,
        )
    )
    await session.commit()
    return event


async def reconcile_orphaned_runs(session: AsyncSession) -> list[Event]:
    active_runs = list(
        await session.scalars(
            select(Run)
            .where(Run.status.in_(ACTIVE_RUN_STATUSES))
            .order_by(Run.id)
        )
    )
    terminal_events: list[Event] = []
    for run in active_runs:
        finalization = await finalize_run(
            session,
            run.id,
            assistant_content=None,
            status="outcome_unknown",
            exit_code=run.exit_code,
            session_id=None,
            error_code="backend_restarted",
        )
        terminal_events.append(finalization.terminal_event)
    return terminal_events


async def list_run_events(
    session: AsyncSession,
    run_id: str,
    *,
    after_seq: int,
    limit: int,
) -> RunEventPage | None:
    run = await session.get(Run, run_id)
    if run is None:
        return None

    result = await session.scalars(
        select(Event)
        .where(Event.run_id == run_id, Event.seq > after_seq)
        .order_by(Event.seq.asc())
        .limit(limit)
    )
    return RunEventPage(
        conversation_id=run.conversation_id,
        run_status=run.status,
        events=list(result),
    )


async def list_conversation_events(
    session: AsyncSession,
    conversation_id: str,
    *,
    offset: int,
    limit: int,
) -> list[Event] | None:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.status == "deleted":
        return None

    result = await session.scalars(
        select(Event)
        .join(Run, Run.id == Event.run_id)
        .where(Run.conversation_id == conversation_id)
        .order_by(Event.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result)


async def list_conversations(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    owner_id: str | None = None,
    standalone: bool = False,
) -> list[Conversation]:
    query = select(Conversation).where(Conversation.status != "deleted")
    if owner_id is not None:
        query = query.where(Conversation.owner_id == owner_id)
    if standalone:
        # Диалоги проектов показываются в «Моих проектах», а не в свободном чате.
        query = query.where(~exists().where(Project.conversation_id == Conversation.id))
    result = await session.scalars(
        query
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result)


async def get_active_run_ids(
    session: AsyncSession,
    conversation_ids: list[str],
) -> dict[str, str]:
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(Run.conversation_id, Run.id).where(
            Run.conversation_id.in_(conversation_ids),
            Run.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    return {conversation_id: run_id for conversation_id, run_id in rows}


async def list_messages(
    session: AsyncSession,
    conversation_id: str,
) -> list[Message] | None:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.status == "deleted":
        return None

    result = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.sequence.asc())
    )
    return list(result)


async def delete_conversation(session: AsyncSession, conversation_id: str) -> str:
    """Мягкое удаление: диалог пропадает из списков. Возвращает deleted | not_found | busy."""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.status == "deleted":
        return "not_found"
    active_run = await session.scalar(
        select(Run.id).where(
            Run.conversation_id == conversation.id,
            Run.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active_run is not None:
        return "busy"
    conversation.status = "deleted"
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
    return "deleted"
