"""Define persisted conversation contracts independently of transport and storage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from komaquill.domain.roleplay import (
    CharacterDefinition,
    ConversationMemory,
    ConversationState,
    PersonDefinition,
    Record,
    StatePatch,
    StoryDefinition,
)


def now() -> datetime:
    return datetime.now(UTC)


class PresetSnapshot(Record):
    id: UUID
    revision: int
    name: str
    image_id: UUID | None = None


class ConversationInput(Record):
    title: str = Field(min_length=1, max_length=120)
    scenario: StoryDefinition = Field(default_factory=StoryDefinition)
    character: CharacterDefinition = Field(default_factory=CharacterDefinition)
    user: PersonDefinition = Field(default_factory=PersonDefinition)
    instructions: str = Field(default="", max_length=20000)
    initial_state: ConversationState = Field(default_factory=ConversationState)
    llm_profile: str = "lmstudio-4b"
    locale: Literal["ja", "en"] = "ja"
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
    narration: str = ""
    state: ConversationState
    image_id: UUID | None = None
    created_at: datetime = Field(default_factory=now)


class Conversation(Record):
    id: UUID = Field(default_factory=uuid4)
    definition: ConversationInput
    state: ConversationState = Field(default_factory=ConversationState)
    memory: ConversationMemory | None = None
    turns: list[Turn] = Field(default_factory=list)
    revision: int = 0
    created_at: datetime = Field(default_factory=now)


class SendInput(Record):
    operation_id: UUID
    text: str = Field(min_length=1, max_length=20000)
    allow_real_llm: bool = False
    locale: Literal["ja", "en"] | None = None


class Reply(Record):
    narration: str = Field(
        default="",
        max_length=20000,
        description="Visible scene, background and character actions in the requested language.",
    )
    text: str = Field(
        min_length=1,
        max_length=20000,
        description=(
            "Spoken words by assistant_character addressed to human_player. "
            "Never repeat the player input or speak as the player."
        ),
    )
    state_patch: StatePatch


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
    state: ConversationState = Field(default_factory=ConversationState)
