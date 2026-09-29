from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies import get_session
from backend.repository import (
    get_active_run_ids,
    list_conversations,
    list_messages,
    list_run_events,
)
from backend.schemas import ConversationRead, EventRead, MessageRead


router = APIRouter(prefix="/api", tags=["history"])
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
