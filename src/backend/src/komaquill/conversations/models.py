"""Define persisted conversation contracts independently of transport and storage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from komaquill.conversations.actions import ActionRecord, PlayerAction, ProposedAction
from komaquill.conversations.visuals import VisualReviewRecord
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
    action_id: UUID | None = None


class Conversation(Record):
    id: UUID = Field(default_factory=uuid4)
    definition: ConversationInput
    state: ConversationState = Field(default_factory=ConversationState)
    memory: ConversationMemory | None = None
    turns: list[Turn] = Field(default_factory=list)
    revision: int = 0
    created_at: datetime = Field(default_factory=now)
    actions: list[ActionRecord] = Field(default_factory=list)
    proposals: list[ProposedAction] = Field(default_factory=list)
    visual_reviews: list[VisualReviewRecord] = Field(default_factory=list)


class SendInput(Record):
    operation_id: UUID
    text: str = Field(default="", max_length=20000)
    action_id: UUID | None = None
    allow_real_llm: bool = False
    locale: Literal["ja", "en"] | None = None

    @model_validator(mode="after")
    def content_required(self) -> "SendInput":
        if not self.text.strip() and self.action_id is None:
            raise ValueError("A message or a confirmed action is required.")
        return self


class Reply(Record):
    narration: str = Field(
        default="",
        max_length=20000,
        description="Visible scene, background and character actions in the requested language.",
    )
    text: str = Field(
        max_length=20000,
        description=(
            "Spoken words by assistant_character addressed to human_player. "
            "Never repeat the player input or speak as the player."
        ),
    )
    state_patch: StatePatch
    action_proposals: list[PlayerAction] = Field(default_factory=list, max_length=4)


class Operation(Record):
    id: UUID
    conversation_id: UUID
    state: Literal["accepted", "running", "complete", "failed", "interrupted"]
    error: str | None = None
    image_state: Literal["disabled", "pending", "submitted", "failed"] = "disabled"
    image_id: UUID | None = None
    image_error: str | None = None
    created_at: datetime = Field(default_factory=now)
