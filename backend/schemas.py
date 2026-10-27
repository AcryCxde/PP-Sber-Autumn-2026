from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class LegacyProjectCreate(BaseModel):
    """Narrow bridge input; it is not the full React B project schema."""

    clientProjectId: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=1, max_length=200)

    model_config = ConfigDict(extra="forbid")


class LegacyProjectCreated(BaseModel):
    id: str
    bridge: Literal["legacy_conversation"]


class LegacyRunCreate(BaseModel):
    """A text-only bridge to the existing legacy run.create lifecycle."""

    content: str = Field(min_length=1, max_length=100_000)
    contextMode: Literal["resume", "reset"] = "resume"
    roles: list[str] | None = None
    stage: str | None = None
    input_files: list[str] | None = None

    model_config = ConfigDict(extra="forbid")


class LegacyRunAnswer(BaseModel):
    questionId: str = Field(min_length=1, max_length=128)
    answers: dict[str, str] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class LegacyRunMessage(BaseModel):
    id: str
    role: Literal["user", "facilitator", "system"]
    text: str
    at: datetime


class LegacyRunSnapshot(BaseModel):
    id: str
    revision: int
    status: Literal["queued", "running", "waiting_for_input", "completed", "failed", "outcome_unknown"]
    stageId: str
    messages: list[LegacyRunMessage]
    artifacts: list[Any]
    question: dict[str, Any] | None = None
