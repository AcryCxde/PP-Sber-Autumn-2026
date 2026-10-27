"""Narrow REST bridge from React B experiments to the legacy conversation runner.

It deliberately exposes only conversations, text runs and snapshots. Files,
saved artifacts, approvals, ZIPs, role execution and clarification/resume are
outside this adapter until their server contracts exist.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies import get_session
from backend.models import Conversation, Event
from backend.repository import (
    RunAcceptanceError,
    accept_run,
    client_request_exists,
    read_conversation_run,
)
from backend.schemas import (
    LegacyProjectCreate,
    LegacyProjectCreated,
    LegacyRunCreate,
    LegacyRunMessage,
    LegacyRunSnapshot,
)


router = APIRouter(prefix="/api/projects", tags=["legacy project bridge"])


def _snapshot(run, messages, revision: int) -> LegacyRunSnapshot:
    status_map = {"pending": "queued", "running": "running", "completed": "completed",
                  "failed": "failed", "outcome_unknown": "outcome_unknown"}
    if run.status not in status_map:
        raise HTTPException(status_code=409, detail="Legacy run has no React B snapshot state")
    role_map = {"user": "user", "assistant": "facilitator", "system": "system"}
    return LegacyRunSnapshot(
        id=run.id,
        revision=revision,
        status=status_map[run.status],
        stageId="legacy-conversation",
        messages=[
            LegacyRunMessage(
                id=item.id,
                role=role_map.get(item.role, "system"),
                text=item.content,
                at=item.created_at,
            )
            for item in messages
        ],
        artifacts=[],
    )


class _RestEventSink:
    """Carries app state for the existing executor while intentionally dropping WS delivery.

    Events are still stored by the legacy executor and are retrieved through GET.
    """

    def __init__(self, state: Any):
        self.app = SimpleNamespace(state=state)

    async def send_text(self, _message: str) -> None:
        return None


def _schedule_reserved(state: Any, coroutine: Any) -> None:
    async def run_with_slot() -> None:
        try:
            await coroutine
        finally:
            async with state.run_slot_lock:
                state.run_task_count -= 1

    task = asyncio.create_task(run_with_slot())
    state.run_tasks.add(task)
    task.add_done_callback(state.run_tasks.discard)


@router.post("", response_model=LegacyProjectCreated, status_code=status.HTTP_201_CREATED)
async def create_project_bridge(
    body: LegacyProjectCreate,
    session: AsyncSession = Depends(get_session),
) -> LegacyProjectCreated:
    existing = await session.get(Conversation, body.clientProjectId)
    if existing is not None:
        if existing.title != body.title:
            raise HTTPException(status_code=409, detail="clientProjectId already belongs to another title")
        return LegacyProjectCreated(id=existing.id, bridge="legacy_conversation")
    session.add(Conversation(id=body.clientProjectId, title=body.title, context_state="new"))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="clientProjectId already exists") from None
    return LegacyProjectCreated(id=body.clientProjectId, bridge="legacy_conversation")


@router.post("/{project_id}/runs", response_model=LegacyRunSnapshot)
async def create_project_run_bridge(
    project_id: str,
    body: LegacyRunCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> LegacyRunSnapshot:
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    state = request.app.state
    async with state.run_slot_lock:
        replay = await client_request_exists(session, key)
        if state.run_task_count >= state.settings.max_concurrent_runs and not replay:
            raise HTTPException(status_code=429, detail="Legacy executor is at capacity")
        try:
            acceptance = await accept_run(
                session,
                conversation_id=project_id,
                client_request_id=key,
                content=body.content,
                requested_context_mode=body.contextMode,
            )
        except RunAcceptanceError as error:
            raise HTTPException(status_code=409, detail=error.code) from None
        if not acceptance.idempotent_replay:
            state.run_task_count += 1

    if not acceptance.idempotent_replay:
        # Local import avoids a startup import cycle: main registers this router.
        from backend.main import handle_run_create

        _schedule_reserved(
            state,
            handle_run_create(
                _RestEventSink(state),
                conversation_id=project_id,
                client_request_id=key,
                content=body.content,
                context_mode=body.contextMode,
                acceptance=acceptance,
            ),
        )
    read = await read_conversation_run(session, project_id, acceptance.run_id)
    if read is None:
        raise HTTPException(status_code=500, detail="Accepted run could not be read")
    return _snapshot(*read)


@router.get("/{project_id}/runs/{run_id}", response_model=LegacyRunSnapshot)
async def get_project_run_bridge(
    project_id: str,
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> LegacyRunSnapshot:
    read = await read_conversation_run(session, project_id, run_id)
    if read is None:
        raise HTTPException(status_code=404, detail="Run not found in project")
    return _snapshot(*read)
