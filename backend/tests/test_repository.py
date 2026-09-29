from sqlalchemy import delete, select

from backend.db import Database
from backend.models import Conversation, Message, Run
from backend.repository import create_request, finalize_run, mark_run_running


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


async def test_sqlite_foreign_keys_cascade_history(tmp_path) -> None:
    database_path = (tmp_path / "foreign-keys.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()

    try:
        async with database.session() as session:
            created = await create_request(session, "Delete me")
            await session.execute(
                delete(Conversation).where(
                    Conversation.id == created.conversation_id
                )
            )
            await session.commit()

        async with database.session() as session:
            assert await session.get(Run, created.run_id) is None
            assert await session.get(Message, created.message_id) is None
    finally:
        await database.dispose()
