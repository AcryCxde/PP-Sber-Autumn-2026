from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import CurrentUser, owns_conversation, owns_run
from backend.config import Settings
from backend.dependencies import get_session
from backend.models import Artifact, Event
from backend.public_events import project_public_event_data
from backend.repository import (
    delete_conversation,
    get_active_run_ids,
    list_conversation_events,
    list_conversations,
    list_messages,
    list_run_events,
)
from backend.schemas import ConversationRead, EventRead, MessageRead


router = APIRouter(prefix="/api", tags=["history"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDependency = Annotated[Settings, Depends(get_app_settings)]


async def require_conversation(
    session: AsyncSession,
    conversation_id: str,
    user: CurrentUser,
) -> None:
    # Чужой диалог неотличим от несуществующего.
    if not await owns_conversation(session, conversation_id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )


async def require_run(session: AsyncSession, run_id: str, user: CurrentUser, detail: str) -> None:
    if not await owns_run(session, run_id, user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def public_event_read(item: Event) -> EventRead:
    projected = project_public_event_data({**item.payload, "type": item.type})
    return EventRead(
        run_id=item.run_id,
        seq=item.seq,
        type=projected["type"],
        payload={key: value for key, value in projected.items() if key != "type"},
        created_at=item.created_at,
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_conversation(
    conversation_id: str,
    request: Request,
    session: SessionDependency,
    user: CurrentUser,
) -> Response:
    await require_conversation(session, conversation_id, user)
    outcome = await delete_conversation(session, conversation_id)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Conversation not found")
    if outcome == "busy":
        raise HTTPException(status_code=409, detail="Conversation has an active run")
    # В режиме runner у диалога есть контейнер и тома сессии: удаляются вместе с ним.
    runner_manager = getattr(request.app.state, "runner_manager", None)
    if runner_manager is not None:
        await runner_manager.discard(
            runner_manager.project_id(conversation_id),
            reason="conversation_deleted",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations", response_model=list[ConversationRead])
async def get_conversations(
    session: SessionDependency,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    standalone: bool = False,
) -> list[ConversationRead]:
    conversations = await list_conversations(
        session,
        limit=limit,
        offset=offset,
        owner_id=user.id,
        standalone=standalone,
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
    user: CurrentUser,
) -> list[MessageRead]:
    await require_conversation(session, conversation_id, user)
    messages = await list_messages(session, conversation_id)
    if messages is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    return [MessageRead.model_validate(item) for item in messages]


@router.get(
    "/conversations/{conversation_id}/events",
    response_model=list[EventRead],
)
async def get_conversation_events(
    conversation_id: str,
    session: SessionDependency,
    user: CurrentUser,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[EventRead]:
    await require_conversation(session, conversation_id, user)
    events = await list_conversation_events(
        session,
        conversation_id,
        offset=offset,
        limit=limit,
    )
    if events is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    return [public_event_read(item) for item in events]


@router.get("/runs/{run_id}/events", response_model=list[EventRead])
async def get_run_events(
    run_id: str,
    session: SessionDependency,
    user: CurrentUser,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventRead]:
    await require_run(session, run_id, user, "Run not found")
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

    return [public_event_read(item) for item in page.events]


@router.get("/runs/{run_id}/events/{event_seq}/file")
async def download_event_file(
    run_id: str,
    event_seq: int,
    session: SessionDependency,
    settings: SettingsDependency,
    user: CurrentUser,
) -> Response:
    await require_run(session, run_id, user, "File artifact not found")
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
        and artifact.size_bytes > settings.artifact_max_download_bytes
    ):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Persisted file snapshot exceeds the download limit",
        )
    if artifact.content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact has no persisted file content",
        )

    content = artifact.content.encode("utf-8")
    if len(content) > settings.artifact_max_download_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
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
