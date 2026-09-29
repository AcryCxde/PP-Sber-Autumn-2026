from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Conversation, Event, Message, Run


ACTIVE_RUN_STATUSES = {"pending", "running"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


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
) -> Event:
    next_seq = await session.scalar(
        update(Run)
        .where(Run.id == run_id)
        .values(next_event_seq=Run.next_event_seq + 1)
        .returning(Run.next_event_seq)
    )
    if next_seq is None:
        raise LookupError(f"Run {run_id} was not found")

    event = Event(
        run_id=run_id,
        seq=next_seq,
        type=str(event_data["type"]),
        payload={key: value for key, value in event_data.items() if key != "type"},
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
) -> RunAcceptance:
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
            title=make_title(content),
            context_state="new",
            next_message_seq=0,
        )
        session.add(conversation)
        await session.flush()
    else:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.status == "deleted":
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
            {
                "type": "context.reset",
                "schema_version": 1,
                "message": message_payload(reset_message),
            },
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
        {
            "type": "run.accepted",
            "schema_version": 1,
            "client_request_id": client_request_id,
            "context_mode": context_mode,
            "message": message_payload(user_message),
        },
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


async def accept_run(
    session: AsyncSession,
    *,
    conversation_id: str | None,
    client_request_id: str,
    content: str,
    requested_context_mode: str = "resume",
) -> RunAcceptance:
    try:
        return await _accept_run_once(
            session,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            content=content,
            requested_context_mode=requested_context_mode,
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
) -> CreatedRequest:
    accepted = await accept_run(
        session,
        conversation_id=None,
        client_request_id=str(uuid4()),
        content=prompt,
    )
    return CreatedRequest(
        conversation_id=accepted.conversation_id,
        run_id=accepted.run_id,
        message_id=accepted.user_message.id,
    )


async def mark_run_running(session: AsyncSession, run_id: str) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"Run {run_id} was not found")
    if run.status != "pending":
        return

    run.status = "running"
    run.started_at = datetime.now(UTC)
    await session.commit()


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
    result_event: Mapping[str, Any] | None = None,
) -> RunFinalization:
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"Run {run_id} was not found")

    conversation = await session.get(Conversation, run.conversation_id)
    if conversation is None:
        raise LookupError(f"Conversation {run.conversation_id} was not found")

    if run.status in TERMINAL_RUN_STATUSES:
        assistant_message = await session.scalar(
            select(Message).where(Message.run_id == run.id, Message.role == "assistant")
        )
        terminal_event = await session.scalar(
            select(Event)
            .where(Event.run_id == run.id, Event.type.in_({"result", "run.failed"}))
            .order_by(Event.seq.desc())
        )
        if terminal_event is None:
            terminal_event = await _append_event_in_transaction(
                session,
                run.id,
                {
                    "type": "result" if run.status == "completed" else "run.failed",
                    "schema_version": 1,
                    "status": run.status,
                    "client_request_id": run.client_request_id,
                    "session_state": conversation.context_state,
                    "error_code": run.error_code,
                    "message": run.error,
                    "assistant_message": (
                        message_payload(assistant_message)
                        if assistant_message
                        else None
                    ),
                },
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
            error = "Claude вернул неожиданный session ID."

    assistant_message: Message | None = None
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
    run.error = error
    run.error_code = error_code
    run.claude_session_id = session_id
    conversation.updated_at = now

    if status == "completed" and session_id:
        conversation.claude_session_id = session_id
        conversation.context_state = "active"
    elif status == "completed":
        conversation.context_state = "unavailable"
    elif error_code in {
        "claude_session_unavailable",
        "claude_session_mismatch",
        "claude_session_state_uncertain",
    }:
        conversation.context_state = "unavailable"

    if status == "completed":
        terminal_data: dict[str, Any] = {
            **(dict(result_event) if result_event else {}),
            "type": "result",
            "schema_version": 1,
            "status": "completed",
            "client_request_id": run.client_request_id,
            "session_state": conversation.context_state,
            "assistant_message": (
                message_payload(assistant_message)
                if assistant_message
                else None
            ),
        }
    else:
        terminal_data = {
            "type": "run.failed",
            "schema_version": 1,
            "status": status,
            "client_request_id": run.client_request_id,
            "error_code": error_code or "claude_cli_failed",
            "message": error or "Запрос Claude завершился с ошибкой.",
            "session_state": conversation.context_state,
            "can_reset_context": conversation.context_state == "unavailable",
        }

    terminal_event = await _append_event_in_transaction(
        session,
        run.id,
        terminal_data,
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


async def list_conversations(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> list[Conversation]:
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.status != "deleted")
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
