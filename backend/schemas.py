from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ConversationRead(BaseModel):
    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MessageRead(BaseModel):
    id: str
    conversation_id: str
    run_id: str | None
    role: str
    sequence: int
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
