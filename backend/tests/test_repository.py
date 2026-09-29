import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from backend.db import Database
from backend.models import Conversation, Event, Message, Run
from backend.repository import (
    RunAcceptanceError,
    accept_run,
    append_event,
    create_request,
    finalize_run,
    list_run_events,
    mark_run_running,
)


async def test_request_lifecycle_is_persisted(tmp_path) -> None:
    database_path = (tmp_path / "repository.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Persist me")

        async with database.session() as session:
            await mark_run_running(session, created.run_id)

        async with database.session() as session:
            finalization = await finalize_run(
                session,
                created.run_id,
                assistant_content="Persisted response",
                status="completed",
                exit_code=0,
                session_id="claude-session",
                duration_ms=200,
                cost_usd=0.5,
                turns=2,
            )

        async with database.session() as session:
            conversation = await session.get(Conversation, created.conversation_id)
            run = await session.get(Run, created.run_id)
            messages = list(
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == created.conversation_id)
                    .order_by(Message.sequence)
                )
            )

        assert conversation is not None
        assert conversation.claude_session_id == "claude-session"
        assert conversation.context_state == "active"
        assert conversation.next_message_seq == 2
        assert run is not None
        assert run.status == "completed"
        assert run.started_at is not None
        assert run.completed_at is not None
        assert run.exit_code == 0
        assert run.duration_ms == 200
        assert run.turns == 2
        assert float(run.cost_usd) == 0.5
        assert finalization.assistant_message is not None
        assert finalization.terminal_event.type == "result"
        assert finalization.terminal_event.payload["assistant_message"]["content"] == "Persisted response"
        assert [(message.sequence, message.role, message.content) for message in messages] == [
            (1, "user", "Persist me"),
            (2, "assistant", "Persisted response"),
        ]
    finally:
        await database.dispose()


async def test_multiple_runs_share_conversation_and_resume_session(tmp_path) -> None:
    database_path = (tmp_path / "multi-turn.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        first_request_id = str(uuid4())
        async with database.session() as session:
            first = await accept_run(
                session,
                conversation_id=None,
                client_request_id=first_request_id,
                content="First turn",
            )

        async with database.session() as session:
            await mark_run_running(session, first.run_id)
            await finalize_run(
                session,
                first.run_id,
                assistant_content="First response",
                status="completed",
                exit_code=0,
                session_id="session-1",
            )

        second_request_id = str(uuid4())
        async with database.session() as session:
            second = await accept_run(
                session,
                conversation_id=first.conversation_id,
                client_request_id=second_request_id,
                content="Second turn",
            )

        assert second.conversation_id == first.conversation_id
        assert second.context_mode == "resume"
        assert second.resume_session_id == "session-1"

        async with database.session() as session:
            await mark_run_running(session, second.run_id)
            await finalize_run(
                session,
                second.run_id,
                assistant_content="Second response",
                status="completed",
                exit_code=0,
                session_id="session-1",
            )

        async with database.session() as session:
            conversations = list(await session.scalars(select(Conversation)))
            messages = list(
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == first.conversation_id)
                    .order_by(Message.sequence)
                )
            )

        assert len(conversations) == 1
        assert [(item.sequence, item.role, item.content) for item in messages] == [
            (1, "user", "First turn"),
            (2, "assistant", "First response"),
            (3, "user", "Second turn"),
            (4, "assistant", "Second response"),
        ]
    finally:
        await database.dispose()


async def test_explicit_context_reset_keeps_chat_and_starts_fresh_session(tmp_path) -> None:
    database_path = (tmp_path / "context-reset.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        async with database.session() as session:
            first = await accept_run(
                session,
                conversation_id=None,
                client_request_id=str(uuid4()),
                content="Initial turn",
            )
        async with database.session() as session:
            await mark_run_running(session, first.run_id)
            await finalize_run(
                session,
                first.run_id,
                assistant_content="Initial response",
                status="completed",
                exit_code=0,
                session_id="old-session",
            )

        async with database.session() as session:
            reset = await accept_run(
                session,
                conversation_id=first.conversation_id,
                client_request_id=str(uuid4()),
                content="Start fresh",
                requested_context_mode="reset",
            )

        assert reset.context_mode == "reset"
        assert reset.resume_session_id == "old-session"
        assert reset.reset_event is not None

        async with database.session() as session:
            conversation = await session.get(Conversation, first.conversation_id)
            messages = list(
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == first.conversation_id)
                    .order_by(Message.sequence)
                )
            )

        assert conversation is not None
        assert conversation.claude_session_id is None
        assert conversation.context_state == "reset"
        assert [(item.sequence, item.role, item.kind) for item in messages] == [
            (1, "user", "text"),
            (2, "assistant", "text"),
            (3, "system", "context_reset"),
            (4, "user", "text"),
        ]
    finally:
        await database.dispose()


async def test_run_acceptance_is_idempotent_and_rejects_busy_conversation(tmp_path) -> None:
    database_path = (tmp_path / "acceptance.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        request_id = str(uuid4())
        async with database.session() as session:
            first = await accept_run(
                session,
                conversation_id=None,
                client_request_id=request_id,
                content="Create once",
            )

        async with database.session() as session:
            replay = await accept_run(
                session,
                conversation_id=None,
                client_request_id=request_id,
                content="Create once",
            )

        assert replay.run_id == first.run_id
        assert replay.conversation_id == first.conversation_id
        assert replay.idempotent_replay is True

        async with database.session() as session:
            with pytest.raises(RunAcceptanceError) as error:
                await accept_run(
                    session,
                    conversation_id=first.conversation_id,
                    client_request_id=str(uuid4()),
                    content="Parallel turn",
                )
        assert error.value.code == "conversation_busy"
    finally:
        await database.dispose()


async def test_concurrent_duplicate_client_request_is_idempotent(tmp_path) -> None:
    database_path = (tmp_path / "concurrent-idempotency.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        request_id = str(uuid4())

        async def accept() -> tuple[str, str, bool]:
            async with database.session() as session:
                accepted = await accept_run(
                    session,
                    conversation_id=None,
                    client_request_id=request_id,
                    content="Same request",
                )
                return (
                    accepted.conversation_id,
                    accepted.run_id,
                    accepted.idempotent_replay,
                )

        first, second = await asyncio.gather(accept(), accept())
        assert first[:2] == second[:2]
        assert sorted([first[2], second[2]]) == [False, True]

        async with database.session() as session:
            assert len(list(await session.scalars(select(Conversation)))) == 1
            assert len(list(await session.scalars(select(Run)))) == 1
    finally:
        await database.dispose()


async def test_events_are_append_only_and_paginated_by_sequence(tmp_path) -> None:
    database_path = (tmp_path / "events.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Stream events")

        async with database.session() as session:
            first = await append_event(
                session,
                created.run_id,
                {"type": "message", "schema_version": 1, "content": "one"},
            )

        async with database.session() as session:
            second = await append_event(
                session,
                created.run_id,
                {"type": "message", "schema_version": 1, "content": "two"},
            )

        assert (first.seq, second.seq) == (2, 3)

        async with database.session() as session:
            page = await list_run_events(
                session,
                created.run_id,
                after_seq=1,
                limit=10,
            )

        assert page is not None
        assert page.conversation_id == created.conversation_id
        assert [event.seq for event in page.events] == [2, 3]

        async with database.session() as session:
            session.add(
                Event(
                    run_id=created.run_id,
                    seq=3,
                    type="message",
                    payload={"schema_version": 1, "content": "duplicate"},
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await database.dispose()


async def test_concurrent_event_appends_allocate_unique_sequences(tmp_path) -> None:
    database_path = (tmp_path / "concurrent-events.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Concurrent stream")

        async def append(index: int) -> Event:
            async with database.session() as session:
                return await append_event(
                    session,
                    created.run_id,
                    {
                        "type": "message",
                        "schema_version": 1,
                        "content": str(index),
                    },
                )

        events = await asyncio.gather(*(append(index) for index in range(20)))
        assert sorted(event.seq for event in events) == list(range(2, 22))
    finally:
        await database.dispose()


async def test_sqlite_foreign_keys_cascade_history(tmp_path) -> None:
    database_path = (tmp_path / "foreign-keys.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Delete me")

        async with database.session() as session:
            stored_event = await append_event(
                session,
                created.run_id,
                {"type": "message", "schema_version": 1, "content": "delete"},
            )

        async with database.session() as session:
            await session.execute(
                delete(Conversation).where(Conversation.id == created.conversation_id)
            )
            await session.commit()

        async with database.session() as session:
            assert await session.get(Run, created.run_id) is None
            assert await session.get(Message, created.message_id) is None
            assert await session.get(Event, stored_event.id) is None
    finally:
        await database.dispose()
