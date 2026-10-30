import hashlib

import pytest
from sqlalchemy import func, select

from backend.db import Database
from backend.models import Artifact, ArtifactConfirmation, Conversation, Event, Project, Run, User
from backend.repository import stage_runner_artifact
from backend.tests.databases import database_url


async def create_runner_run(database: Database) -> tuple[str, str]:
    async with database.session() as session:
        user = User(email="artifact-integrity@example.com", password_hash="x")
        conversation = Conversation(title="Artifact integrity")
        session.add_all([user, conversation])
        await session.flush()
        project = Project(owner_id=user.id, conversation_id=conversation.id)
        run = Run(conversation_id=conversation.id, client_request_id="artifact-integrity-request")
        session.add_all([project, run])
        await session.commit()
        return run.id, project.id


@pytest.mark.parametrize(
    ("source_bytes", "max_bytes"),
    [
        (b"ASCII", 5),
        ("много байтов".encode(), len("много байтов".encode())),
        (b"one\r\ntwo\r\n", 10),
        (b"\xef\xbb\xbfBOM", 6),
        (b"", 0),
    ],
)
async def test_stage_runner_artifact_persists_exact_utf8_snapshot(
    tmp_path,
    source_bytes: bytes,
    max_bytes: int,
) -> None:
    database = Database(database_url(tmp_path / "exact-artifact.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_runner_run(database)
        content = source_bytes.decode("utf-8")
        async with database.session() as session:
            staged = await stage_runner_artifact(
                session,
                run_id,
                project_id=project_id,
                transport_run_id=run_id,
                artifact_id="artifact-1",
                path="result.txt",
                content=content,
                source_bytes=source_bytes,
                version_id="version-1",
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                max_bytes=max_bytes,
            )
            assert staged.event.payload == {
                "schema_version": 1,
                "path": "result.txt",
                "size_bytes": len(source_bytes),
                "truncated": False,
                "num_lines": content.count("\n") + 1,
            }
            assert staged.receipt == {
                "artifact_id": "artifact-1",
                "version_id": "version-1",
                "size_bytes": len(source_bytes),
                "download": f"/api/runs/{run_id}/events/{staged.event.seq}/file",
            }
            with pytest.raises(TypeError):
                staged.receipt["artifact_id"] = "changed"  # type: ignore[index]
            await session.commit()

        async with database.session() as session:
            artifact = await session.scalar(select(Artifact))
            confirmation = await session.scalar(select(ArtifactConfirmation))
            assert artifact is not None and confirmation is not None
            assert artifact.content == content
            assert artifact.content.encode("utf-8") == source_bytes
            assert artifact.size_bytes == len(source_bytes)
            assert artifact.truncated is False
            assert confirmation.receipt == staged.receipt
            assert confirmation.sha256 == hashlib.sha256(source_bytes).hexdigest()
            assert confirmation.size_bytes == len(source_bytes)
            assert confirmation.status == "pending"
    finally:
        await database.dispose()


@pytest.mark.parametrize(
    ("source_bytes", "content", "sha256", "max_bytes"),
    [
        (b"\xff", "", hashlib.sha256(b"\xff").hexdigest(), None),
        (b"bytes", "other", hashlib.sha256(b"bytes").hexdigest(), None),
        (b"bytes", "bytes", "0" * 64, None),
        (b"four", "four", hashlib.sha256(b"four").hexdigest(), 3),
        (b"nul\x00byte", "nul\x00byte", hashlib.sha256(b"nul\x00byte").hexdigest(), None),
    ],
)
async def test_stage_runner_artifact_rejects_invalid_input_without_writes(
    tmp_path,
    source_bytes: bytes,
    content: str,
    sha256: str,
    max_bytes: int | None,
) -> None:
    database = Database(database_url(tmp_path / "invalid-artifact.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_runner_run(database)
        async with database.session() as session:
            with pytest.raises(ValueError):
                await stage_runner_artifact(
                    session,
                    run_id,
                    project_id=project_id,
                    transport_run_id=run_id,
                    artifact_id="artifact-1",
                    path="result.txt",
                    content=content,
                    source_bytes=source_bytes,
                    version_id="version-1",
                    sha256=sha256,
                    max_bytes=max_bytes,
                )
            await session.commit()

        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
            assert await session.scalar(select(func.count()).select_from(Event)) == 0
            assert await session.scalar(select(func.count()).select_from(ArtifactConfirmation)) == 0
    finally:
        await database.dispose()


async def test_stage_runner_artifact_caller_rollback_leaves_no_rows(tmp_path) -> None:
    database = Database(database_url(tmp_path / "rollback-artifact.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_runner_run(database)
        source_bytes = b"atomic"
        async with database.session() as session:
            staged = await stage_runner_artifact(
                session,
                run_id,
                project_id=project_id,
                transport_run_id=run_id,
                artifact_id="artifact-1",
                path="result.txt",
                content="atomic",
                source_bytes=source_bytes,
                version_id="version-1",
                sha256=hashlib.sha256(source_bytes).hexdigest(),
            )
            assert staged.event.seq == 1
            await session.rollback()

        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
            assert await session.scalar(select(func.count()).select_from(Event)) == 0
            assert await session.scalar(select(func.count()).select_from(ArtifactConfirmation)) == 0
    finally:
        await database.dispose()


async def test_duplicate_artifact_identity_is_idempotent_and_conflicts_are_rejected(tmp_path) -> None:
    database = Database(database_url(tmp_path / "duplicate-artifact.db"))
    await database.create_schema()
    try:
        run_id, project_id = await create_runner_run(database)
        source_bytes = b"duplicate"
        kwargs = {
            "project_id": project_id,
            "transport_run_id": run_id,
            "artifact_id": "artifact-1",
            "path": "result.txt",
            "content": "duplicate",
            "source_bytes": source_bytes,
            "version_id": "version-1",
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
        async with database.session() as session:
            await stage_runner_artifact(session, run_id, **kwargs)
            await session.commit()

        async with database.session() as session:
            replay = await stage_runner_artifact(session, run_id, **kwargs)
            assert replay.created is False
            await session.commit()

        async with database.session() as session:
            with pytest.raises(ValueError):
                await stage_runner_artifact(session, run_id, **{**kwargs, "version_id": "version-2"})
            await session.rollback()

        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 1
            assert await session.scalar(select(func.count()).select_from(Event)) == 1
            assert await session.scalar(select(func.count()).select_from(ArtifactConfirmation)) == 1
    finally:
        await database.dispose()
