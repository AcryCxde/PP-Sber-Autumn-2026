from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ConversationRead(BaseModel):
    id: str
    title: str
    status: str
    context_state: str
    active_run_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MessageRead(BaseModel):
    id: str
    conversation_id: str
    run_id: str | None
    role: str
    kind: str
    sequence: int
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EventRead(BaseModel):
    run_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
