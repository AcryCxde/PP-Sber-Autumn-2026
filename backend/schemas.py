from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Credentials(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class UserRead(BaseModel):
    id: str
    email: str
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


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
