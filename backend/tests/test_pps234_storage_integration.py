"""PPS-234: legacy ProjectRun storage projection, not ProductRun/V2 delivery."""

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.db import Database
from backend.models import Artifact, Project, ProjectRun, Run, User
from backend.projects import create_project, stage_phase, stage_runs
from backend.repository import (
    accept_run,
    append_artifact,
    append_event,
    finalize_run,
    list_run_events,
    mark_run_running,
)
from backend.tests.databases import database_url


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(database_url(tmp_path / "pps-234-storage.db"))
    try:
        await database.create_schema()
        yield database
    finally:
        await database.dispose()


async def _project(database: Database, owner_id: str, name: str) -> Project:
    async with database.session() as session:
        project = await create_project(session, owner_id, example=False)
        project.name = name
        project.problem = "PPS-234 storage integration probe"
        project.status = "active"
        await session.commit()
        return project


async def _linked_run(database: Database, project: Project, operation_id: str) -> str:
    async with database.session() as session:
        accepted = await accept_run(
            session,
            conversation_id=project.conversation_id,
            client_request_id=operation_id,
            content="Produce a synthetic first result",
        )
        # Legacy stage linkage; no V2 ProductRun is created by this probe.
        session.add(
            ProjectRun(
                run_id=accepted.run_id, project_id=project.id, stage=0, kind="stage"
            )
        )
        await session.commit()
        return accepted.run_id


async def test_pps234_project_run_replay_terminal_and_snapshot_probe(
    database: Database,
) -> None:
    operation_id = "pps-234-op-0001"
    artifact_path = "results/first-result.md"
    artifact_content = "# PPS-234\nSynthetic result: готово.\n"
    artifact_bytes = artifact_content.encode("utf-8")
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    questions = [{"text": "Approve the first result?", "options": []}]

    async with database.session() as session:
        owner = User(
            email="pps-234-probe@example.invalid", password_hash="not-used-by-probe"
        )
        session.add(owner)
        await session.commit()
        owner_id = owner.id

    waiting_project = await _project(database, owner_id, "Waiting project")
    waiting_run_id = await _linked_run(database, waiting_project, operation_id)
    async with database.session() as session:
        assert await mark_run_running(session, waiting_run_id) is not None
    async with database.session() as session:
        runs = await stage_runs(session, waiting_project)
        assert len(runs) == 1
        linked = runs[0]
        assert linked.link.project_id == waiting_project.id
        assert linked.link.run_id == linked.run.id == waiting_run_id
        assert linked.link.product_run_id is None
        assert linked.run.conversation_id == waiting_project.conversation_id
        assert linked.run.status == "running"
        assert stage_phase(waiting_project, runs) == ("working", linked)

    async with database.session() as session:
        artifact_event = await append_artifact(
            session,
            waiting_run_id,
            path=artifact_path,
            content=artifact_content,
            size_bytes=len(artifact_bytes),
            truncated=False,
            num_lines=2,
        )
    async with database.session() as session:
        await append_event(
            session,
            waiting_run_id,
            {"type": "question.asked", "schema_version": 1, "questions": questions},
        )
        await finalize_run(
            session,
            waiting_run_id,
            assistant_content="First result is ready for review.",
            status="completed",
            exit_code=0,
            session_id="pps-234-session",
        )

    async with database.session() as session:
        runs = await stage_runs(session, waiting_project)
        assert runs[0].run.status == "completed"
        assert runs[0].question == [
            {**questions[0], "header": None, "multi_select": False}
        ]
        assert stage_phase(waiting_project, runs)[0] == "waiting"
        snapshot = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == waiting_run_id,
                Artifact.event_seq == artifact_event.seq,
            )
        )
        assert snapshot is not None
        assert artifact_event.type == "artifact.saved"
        assert snapshot.path == artifact_path
        assert snapshot.size_bytes == len(artifact_bytes)
        assert snapshot.content == artifact_content
        assert snapshot.truncated is False
        assert snapshot.sha256 == artifact_sha256
        assert (
            hashlib.sha256(snapshot.content.encode("utf-8")).hexdigest()
            == snapshot.sha256
        )

    async with database.session() as session:
        replay = await accept_run(
            session,
            conversation_id=waiting_project.conversation_id,
            client_request_id=operation_id,
            content="Produce a synthetic first result",
        )
        assert replay.idempotent_replay is True
        assert replay.run_id == waiting_run_id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.client_request_id == operation_id)
            )
            == 1
        )

    async with database.session() as session:
        first_read = await list_run_events(
            session, waiting_run_id, after_seq=0, limit=100
        )
    async with database.session() as session:
        replay_read = await list_run_events(
            session, waiting_run_id, after_seq=0, limit=100
        )
    assert first_read is not None and replay_read is not None
    expected = [(event.seq, event.type, event.payload) for event in first_read.events]
    assert expected
    assert [event.seq for event in first_read.events] == sorted(
        {event.seq for event in first_read.events}
    )
    assert expected[-1][1] == "run.completed"
    assert [
        (event.seq, event.type, event.payload) for event in replay_read.events
    ] == expected
    cursor = first_read.events[0].seq
    async with database.session() as session:
        resumed = await list_run_events(
            session, waiting_run_id, after_seq=cursor, limit=100
        )
    assert resumed is not None
    assert [
        (event.seq, event.type, event.payload) for event in resumed.events
    ] == expected[1:]

    async with database.session() as session:
        with pytest.raises(ValueError, match="already terminal"):
            await append_event(
                session,
                waiting_run_id,
                {"type": "message.created", "schema_version": 1, "content": "late"},
            )
        await session.rollback()
    async with database.session() as session:
        assert await mark_run_running(session, waiting_run_id) is None
    async with database.session() as session:
        guarded = await list_run_events(session, waiting_run_id, after_seq=0, limit=100)
        assert guarded is not None and guarded.run_status == "completed"
        assert [
            (event.seq, event.type, event.payload) for event in guarded.events
        ] == expected

    error_project = await _project(database, owner_id, "Error project")
    error_run_id = await _linked_run(database, error_project, "pps-234-op-error")
    async with database.session() as session:
        assert await mark_run_running(session, error_run_id) is not None
        failed = await finalize_run(
            session,
            error_run_id,
            assistant_content=None,
            status="failed",
            exit_code=1,
            error_code="result_error",
        )
        assert failed.terminal_event.type == "run.failed"
    async with database.session() as session:
        runs = await stage_runs(session, error_project)
        assert runs[0].run.status == "failed"
        assert runs[0].run.error_code == "result_error"
        assert stage_phase(error_project, runs)[0] == "failed"

    completed_project = await _project(database, owner_id, "Completed project")
    completed_run_id = await _linked_run(
        database, completed_project, "pps-234-op-completed"
    )
    async with database.session() as session:
        assert await mark_run_running(session, completed_run_id) is not None
        await append_event(
            session,
            completed_run_id,
            {"type": "message.created", "schema_version": 1, "content": "Done"},
        )
        await finalize_run(
            session,
            completed_run_id,
            assistant_content="Done",
            status="completed",
            exit_code=0,
            session_id="pps-234-completed-session",
        )
    async with database.session() as session:
        runs = await stage_runs(session, completed_project)
        assert runs[0].run.status == "completed"
        assert runs[0].question is None
        assert runs[0].content == "Done"
        assert stage_phase(completed_project, runs)[0] == "review"
