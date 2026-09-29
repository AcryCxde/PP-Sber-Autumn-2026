from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.dependencies import get_session
from backend.models import Artifact
from backend.repository import (
    get_active_run_ids,
    list_conversations,
    list_messages,
    list_run_events,
)
from backend.schemas import ConversationRead, EventRead, MessageRead


router = APIRouter(prefix="/api", tags=["history"])
API_SETTINGS = Settings()
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("/conversations", response_model=list[ConversationRead])
async def get_conversations(
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationRead]:
    conversations = await list_conversations(
        session,
        limit=limit,
        offset=offset,
    )
    active_runs = await get_active_run_ids(
        session,
        [item.id for item in conversations],
    )
    return [
        ConversationRead.model_validate(item).model_copy(
            update={"active_run_id": active_runs.get(item.id)}
        )
        for item in conversations
    ]


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageRead],
)
async def get_conversation_messages(
    conversation_id: str,
    session: SessionDependency,
) -> list[MessageRead]:
    messages = await list_messages(session, conversation_id)
    if messages is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    return [MessageRead.model_validate(item) for item in messages]


@router.get("/runs/{run_id}/events", response_model=list[EventRead])
async def get_run_events(
    run_id: str,
    session: SessionDependency,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventRead]:
    page = await list_run_events(
        session,
        run_id,
        after_seq=after_seq,
        limit=limit,
    )
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found",
        )

    return [EventRead.model_validate(item) for item in page.events]


@router.get("/runs/{run_id}/events/{event_seq}/file")
async def download_event_file(
    run_id: str,
    event_seq: int,
    session: SessionDependency,
) -> Response:
    artifact = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run_id,
            Artifact.event_seq == event_seq,
        )
    )
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File artifact not found",
        )
    if artifact.truncated or (
        artifact.size_bytes is not None
        and artifact.size_bytes > API_SETTINGS.artifact_max_download_bytes
    ):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Persisted file snapshot exceeds the download limit",
        )
    if artifact.content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact has no persisted file content",
        )

    content = artifact.content.encode("utf-8")
    if len(content) > API_SETTINGS.artifact_max_download_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is too large to download",
        )

    filename = artifact.path.replace("\\", "/").rsplit("/", 1)[-1]
    filename = filename.replace("\r", "").replace("\n", "") or "artifact"
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
            )
        },
    )
