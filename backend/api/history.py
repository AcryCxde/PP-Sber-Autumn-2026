from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies import get_session
from backend.repository import list_conversations, list_messages
from backend.schemas import ConversationRead, MessageRead


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
    return [ConversationRead.model_validate(item) for item in conversations]


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
