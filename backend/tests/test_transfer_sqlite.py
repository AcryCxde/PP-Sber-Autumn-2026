import pytest
from sqlalchemy import select

from backend.db import Database
from backend.models import Artifact, Event, Project, User
from backend.projects import create_project
from backend.repository import append_event, create_request
from backend.tests.databases import POSTGRES_URL, database_url
from backend.transfer_sqlite import TransferError, transfer


pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="нужен TEST_POSTGRES_URL")


async def test_transfer_copies_history_and_keeps_sequences_usable(tmp_path) -> None:
    source_path = tmp_path / "history.db"
    source = Database(f"sqlite+aiosqlite:///{source_path.as_posix()}")
    await source.migrate()
    try:
        async with source.session() as session:
            user = User(email="a@example.com", password_hash="x")
            session.add(user)
            await session.commit()
            created = await create_request(session, "Привет", owner_id=user.id)
        async with source.session() as session:
            event = await append_event(
                session, created.run_id,
                {"type": "message.created", "schema_version": 1, "content": "ok"},
            )
            session.add(Artifact(run_id=created.run_id, event_seq=event.seq, path="a.txt",
                                 content="bin\x00ary", size_bytes=7))
            await session.commit()
        async with source.session() as session:
            project = await create_project(session, user.id, example=True)
    finally:
        await source.dispose()

    target_url = database_url(tmp_path / "unused.db")
    counts = await transfer(source_path, target_url)
    assert counts["users"] == 1 and counts["projects"] == 1 and counts["artifacts"] == 1

    target = Database(target_url)
    try:
        async with target.session() as session:
            stored = await session.get(User, user.id)
            assert stored.created_at.tzinfo is not None and stored.role == "user"
            assert (await session.scalar(select(Artifact.content))) == "bin�ary"
            assert (await session.get(Project, project.id)).name == project.name
            # Последовательность id событий продолжается после перенесённых строк.
            appended = await append_event(
                session, created.run_id,
                {"type": "message.created", "schema_version": 1, "content": "after"},
            )
            assert appended.id > event.id
            assert len(list(await session.scalars(select(Event)))) == counts["events"] + 1
    finally:
        await target.dispose()

    with pytest.raises(TransferError, match="не пуста"):
        await transfer(source_path, target_url)
