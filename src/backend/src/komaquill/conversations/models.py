"""Define persisted conversation contracts independently of transport and storage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def now() -> datetime:
    return datetime.now(UTC)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PresetSnapshot(Record):
    id: UUID
    revision: int
    name: str
    image_id: UUID | None = None


class ConversationInput(Record):
    title: str = Field(min_length=1, max_length=120)
    scenario: str = Field(default="", max_length=20000)
    character: str = Field(default="", max_length=20000)
    user: str = Field(default="", max_length=20000)
    instructions: str = Field(default="", max_length=20000)
    initial_state: dict[str, str] = Field(default_factory=dict)
    images: bool = False
    llm_profile: str = "fake"
    locale: Literal["ja", "en"] = "en"
    response_length: Literal["short", "standard", "detailed"] = "standard"
    presets: dict[str, PresetSnapshot] = Field(default_factory=dict)
    sample: bool = False


class ConnectionProfile(Record):
    id: str
    provider: Literal["fake", "openai-compatible"]
    model: str


class Turn(Record):
    operation_id: UUID
    user: str
    assistant: str
    state: dict[str, str]
    image_id: UUID | None = None
    scene: str | None = None
    created_at: datetime = Field(default_factory=now)


class Conversation(Record):
    id: UUID = Field(default_factory=uuid4)
    definition: ConversationInput
    state: dict[str, str] = Field(default_factory=dict)
    turns: list[Turn] = Field(default_factory=list)
    revision: int = 0
    created_at: datetime = Field(default_factory=now)


class SendInput(Record):
    operation_id: UUID
    text: str = Field(min_length=1, max_length=20000)
    allow_real_llm: bool = False
    locale: Literal["ja", "en"] | None = None
    images: bool | None = None
    response_length: Literal["short", "standard", "detailed"] | None = None


class Reply(Record):
    text: str = Field(
        min_length=1,
        max_length=20000,
        description="Visible dialogue in the explicitly requested response language.",
    )
    state: dict[str, str]
    scene: str | None = Field(
        description="English visual description including appearance and setting, or null."
    )


class Operation(Record):
    id: UUID
    conversation_id: UUID
    state: Literal["accepted", "running", "complete", "failed", "interrupted"]
    error: str | None = None
    image_state: Literal["disabled", "pending", "submitted", "failed"] = "disabled"
    image_id: UUID | None = None
    image_error: str | None = None
    created_at: datetime = Field(default_factory=now)


class StateCommand(Record):
    command: Literal["reset_state", "set_state"]
    expected_revision: int = Field(ge=0)
    state: dict[str, str] = Field(default_factory=dict)
