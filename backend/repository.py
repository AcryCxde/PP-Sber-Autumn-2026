from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Conversation, Event, Message, Run


@dataclass(frozen=True)
class CreatedRequest:
    conversation_id: str
    run_id: str
    message_id: str


@dataclass(frozen=True)
class RunEventPage:
    conversation_id: str
    events: list[Event]


def make_title(prompt: str, max_length: int = 80) -> str:
    title = " ".join(prompt.split())
    if len(title) <= max_length:
        return title
    return f"{title[: max_length - 1].rstrip()}…"


async def create_request(
    session: AsyncSession,
    prompt: str,
) -> CreatedRequest:
    conversation = Conversation(title=make_title(prompt))
    run = Run(conversation=conversation, status="pending")
    message = Message(
        conversation=conversation,
        run=run,
        role="user",
        sequence=1,
        content=prompt,
    )
    session.add(conversation)
    await session.commit()

    return CreatedRequest(
        conversation_id=conversation.id,
        run_id=run.id,
        message_id=message.id,
    )


async def mark_run_running(session: AsyncSession, run_id: str) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"Run {run_id} was not found")

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
) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"Run {run_id} was not found")

    conversation = await session.get(Conversation, run.conversation_id)
    if conversation is None:
        raise LookupError(f"Conversation {run.conversation_id} was not found")

    if assistant_content:
        session.add(
            Message(
                conversation_id=conversation.id,
                run_id=run.id,
                role="assistant",
                sequence=2,
                content=assistant_content,
            )
        )

    now = datetime.now(UTC)
    run.status = status
    run.completed_at = now
    run.exit_code = exit_code
    run.duration_ms = duration_ms
    run.cost_usd = Decimal(str(cost_usd)) if cost_usd is not None else None
    run.turns = turns
    run.error = error
    conversation.updated_at = now

    if session_id:
        conversation.claude_session_id = session_id

    await session.commit()


async def append_event(
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
        payload={
            key: value
            for key, value in event_data.items()
            if key != "type"
        },
    )
    session.add(event)
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
