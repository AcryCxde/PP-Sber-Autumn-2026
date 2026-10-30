from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.history import SessionDependency
from backend.auth import CurrentUser
from backend.materials import MAX_FILE_BYTES, MaterialError, check_upload
from backend.models import Conversation, ConversationFile, Project
from backend.repository import NEW_CONVERSATION_TITLE
from backend.schemas import ConversationRead


router = APIRouter(prefix="/api", tags=["conversation-files"])


class FileRead(BaseModel):
    id: str
    name: str
    size_bytes: int


async def require_standalone(
    session: AsyncSession,
    conversation_id: str,
    user: CurrentUser,
) -> Conversation:
    """Свой неудалённый свободный диалог; у диалога проекта файлы — в самом проекте."""
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.owner_id == user.id,
            Conversation.status != "deleted",
            ~exists().where(Project.conversation_id == Conversation.id),
        )
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


async def file_list(session: AsyncSession, conversation_id: str) -> list[FileRead]:
    rows = await session.execute(
        select(ConversationFile.id, ConversationFile.name, ConversationFile.size_bytes)
        .where(ConversationFile.conversation_id == conversation_id)
        .order_by(ConversationFile.created_at, ConversationFile.name)
    )
    return [FileRead(id=id_, name=name, size_bytes=size) for id_, name, size in rows]


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(session: SessionDependency, user: CurrentUser) -> ConversationRead:
    """Пустой диалог, чтобы приложить документы до первого сообщения."""
    conversation = Conversation(owner_id=user.id, title=NEW_CONVERSATION_TITLE, context_state="new")
    session.add(conversation)
    await session.commit()
    return ConversationRead.model_validate(conversation)


@router.get("/conversations/{conversation_id}/files", response_model=list[FileRead])
async def get_files(conversation_id: str, session: SessionDependency, user: CurrentUser) -> list[FileRead]:
    await require_standalone(session, conversation_id, user)
    return await file_list(session, conversation_id)


@router.post(
    "/conversations/{conversation_id}/files",
    response_model=list[FileRead],
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    conversation_id: str,
    session: SessionDependency,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
) -> list[FileRead]:
    await require_standalone(session, conversation_id, user)
    current = list(await session.scalars(
        select(ConversationFile).where(ConversationFile.conversation_id == conversation_id)
    ))
    data = await file.read(MAX_FILE_BYTES + 1)
    try:
        name = check_upload(file.filename, data, ((item.name, item.size_bytes) for item in current))
    except MaterialError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None
    existing = next((item for item in current if item.name == name), None)
    if existing is not None:
        # Повторная загрузка файла с тем же именем заменяет его.
        await session.delete(existing)
        await session.flush()
    session.add(ConversationFile(
        conversation_id=conversation_id, name=name, size_bytes=len(data), content=data,
    ))
    await session.commit()
    return await file_list(session, conversation_id)


@router.get("/conversations/{conversation_id}/files/{file_id}")
async def download_file(
    conversation_id: str,
    file_id: str,
    session: SessionDependency,
    user: CurrentUser,
) -> Response:
    await require_standalone(session, conversation_id, user)
    item = await session.scalar(
        select(ConversationFile).where(
            ConversationFile.id == file_id,
            ConversationFile.conversation_id == conversation_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return Response(
        content=item.content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(item.name, safe='')}"},
    )


@router.delete("/conversations/{conversation_id}/files/{file_id}", response_model=list[FileRead])
async def delete_file(
    conversation_id: str,
    file_id: str,
    session: SessionDependency,
    user: CurrentUser,
) -> list[FileRead]:
    await require_standalone(session, conversation_id, user)
    item = await session.scalar(
        select(ConversationFile).where(
            ConversationFile.id == file_id,
            ConversationFile.conversation_id == conversation_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    await session.delete(item)
    await session.commit()
    return await file_list(session, conversation_id)
