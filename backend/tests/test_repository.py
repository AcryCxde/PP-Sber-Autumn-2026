import asyncio

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from backend.db import Database
from backend.models import Conversation, Event, Message, Run
from backend.repository import (
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
            await finalize_run(
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
        assert run is not None
        assert run.status == "completed"
        assert run.started_at is not None
        assert run.completed_at is not None
        assert run.exit_code == 0
        assert run.duration_ms == 200
        assert run.turns == 2
        assert float(run.cost_usd) == 0.5
        assert [(message.role, message.content) for message in messages] == [
            ("user", "Persist me"),
            ("assistant", "Persisted response"),
        ]
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

        assert (first.seq, second.seq) == (1, 2)

        async with database.session() as session:
            page = await list_run_events(
                session,
                created.run_id,
                after_seq=1,
                limit=10,
            )

        assert page is not None
        assert page.conversation_id == created.conversation_id
        assert [event.seq for event in page.events] == [2]
        assert page.events[0].payload["content"] == "two"

        async with database.session() as session:
            session.add(
                Event(
                    run_id=created.run_id,
                    seq=2,
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

        assert sorted(event.seq for event in events) == list(range(1, 21))

        async with database.session() as session:
            page = await list_run_events(
                session,
                created.run_id,
                after_seq=0,
                limit=100,
            )

        assert page is not None
        assert [event.seq for event in page.events] == list(range(1, 21))
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
                delete(Conversation).where(
                    Conversation.id == created.conversation_id
                )
            )
            await session.commit()

        async with database.session() as session:
            assert await session.get(Run, created.run_id) is None
            assert await session.get(Message, created.message_id) is None
            assert await session.get(Event, stored_event.id) is None
    finally:
        await database.dispose()
