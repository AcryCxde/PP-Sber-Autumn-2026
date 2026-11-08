"""PPS-234 storage-side integration probe for Project/Run/event lifecycle."""

import hashlib
import json

import pytest
from sqlalchemy import func, select

from backend.db import Database
from backend.models import Artifact, Event, Project, ProjectRun, Run, User
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


async def _project(database: Database, owner_id: str, name: str) -> Project:
    async with database.session() as session:
        project = await create_project(session, owner_id, example=False)
        project.name = name
        project.problem = "PPS-234 storage integration probe"
        project.status = "active"
        await session.commit()
        return project


async def _linked_run(
    database: Database,
    project: Project,
    operation_id: str,
    prompt: str,
) -> str:
    async with database.session() as session:
        accepted = await accept_run(
            session,
            conversation_id=project.conversation_id,
            client_request_id=operation_id,
            content=prompt,
        )
        session.add(
            ProjectRun(
                run_id=accepted.run_id,
                project_id=project.id,
                stage=0,
                kind="stage",
            )
        )
        await session.commit()
        return accepted.run_id


async def test_pps234_project_run_replay_terminal_and_receipt_probe(tmp_path) -> None:
    database = Database(database_url(tmp_path / "pps-234-storage.db"))
    await database.create_schema()
    operation_id = "pps-234-op-0001"
    artifact_path = "results/first-result.md"
    artifact_content = "# PPS-234\nConfirmed synthetic first result.\n"
    artifact_bytes = artifact_content.encode("utf-8")
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

    try:
        async with database.session() as session:
            owner = User(
                email="pps-234-probe@example.invalid",
                password_hash="not-used-by-probe",
            )
            session.add(owner)
            await session.commit()
            owner_id = owner.id

        waiting_project = await _project(database, owner_id, "Waiting project")
        waiting_run_id = await _linked_run(
            database, waiting_project, operation_id, "Produce the first result"
        )

        async with database.session() as session:
            started = await mark_run_running(session, waiting_run_id)
            assert started is not None
        async with database.session() as session:
            running = await session.get(Run, waiting_run_id)
            assert running is not None and running.status == "running"

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
                {
                    "type": "question.asked",
                    "schema_version": 1,
                    "questions": [{"text": "Approve the first result?", "options": []}],
                },
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
            stored_project = await session.get(Project, waiting_project.id)
            assert stored_project is not None
            phase, _ = stage_phase(stored_project, await stage_runs(session, stored_project))
            assert phase == "waiting"  # UI waiting_for_input projection.

            receipt = await session.scalar(
                select(Artifact).where(
                    Artifact.run_id == waiting_run_id,
                    Artifact.event_seq == artifact_event.seq,
                )
            )
            assert receipt is not None
            assert receipt.path == artifact_path
            assert receipt.size_bytes == len(artifact_bytes)
            assert receipt.content == artifact_content
            assert hashlib.sha256(receipt.content.encode("utf-8")).hexdigest() == artifact_sha256

        async with database.session() as session:
            replay = await accept_run(
                session,
                conversation_id=waiting_project.conversation_id,
                client_request_id=operation_id,
                content="Produce the first result",
            )
            assert replay.idempotent_replay is True
            assert replay.run_id == waiting_run_id
            run_count = await session.scalar(
                select(func.count()).select_from(Run).where(Run.client_request_id == operation_id)
            )
            assert run_count == 1

        async with database.session() as session:
            first_read = await list_run_events(session, waiting_run_id, after_seq=0, limit=100)
        async with database.session() as session:
            replay_read = await list_run_events(session, waiting_run_id, after_seq=0, limit=100)
        assert first_read is not None and replay_read is not None
        assert [(event.seq, event.type) for event in first_read.events] == [
            (event.seq, event.type) for event in replay_read.events
        ]

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

        error_project = await _project(database, owner_id, "Error project")
        error_run_id = await _linked_run(
            database, error_project, "pps-234-op-error", "Fail synthetically"
        )
        async with database.session() as session:
            await mark_run_running(session, error_run_id)
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
            stored_project = await session.get(Project, error_project.id)
            assert stored_project is not None
            phase, _ = stage_phase(stored_project, await stage_runs(session, stored_project))
            assert phase == "failed"  # UI error projection.

        completed_project = await _project(database, owner_id, "Completed project")
        completed_run_id = await _linked_run(
            database, completed_project, "pps-234-op-completed", "Complete synthetically"
        )
        async with database.session() as session:
            await mark_run_running(session, completed_run_id)
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
            stored_project = await session.get(Project, completed_project.id)
            assert stored_project is not None
            phase, _ = stage_phase(stored_project, await stage_runs(session, stored_project))
            assert phase == "review"  # UI completed/result-ready projection.

        evidence = {
            "operation_id": operation_id,
            "run_id": waiting_run_id,
            "artifact_event_seq": artifact_event.seq,
            "path": artifact_path,
            "size_bytes": len(artifact_bytes),
            "sha256_recomputed_from_stored_bytes": artifact_sha256,
            "event_count": len(first_read.events),
            "replay_same_run": True,
            "terminal_guard": "rejected_late_append",
            "ui_phases": ["working", "waiting", "failed", "review"],
            "durable_receipt_limit": "sha256 and confirmed-write boundary are not persisted on base SHA",
        }
        print("PPS234_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
    finally:
        await database.dispose()
