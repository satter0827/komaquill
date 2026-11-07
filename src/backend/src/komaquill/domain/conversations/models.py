"""Define persisted conversation contracts independently of transport and storage."""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, model_validator
from pydantic.json_schema import SkipJsonSchema

from komaquill.domain.conversations.actions import (
    ActionRecord,
    InferredMove,
    PlayerAction,
    ProposedAction,
)
from komaquill.domain.conversations.meetings import MeetingAnswer, MeetingChange, MeetingPlan
from komaquill.domain.conversations.visuals import VisualReviewRecord
from komaquill.domain.errors import OperationStage
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
    tone: str = Field(default="", max_length=2000)
    topics: str = Field(default="", max_length=3000)
    avoid: str = Field(default="", max_length=3000)
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
    action_narration: str = ""
    notes: list[str] = Field(default_factory=list)
    operation_id: UUID
    user: str
    assistant: str
    narration: str = ""
    state: ConversationState
    image_id: UUID | None = None
    created_at: datetime = Field(default_factory=now)
    action_id: UUID | None = None
    action: PlayerAction | None = None
    meeting: MeetingAnswer | None = None
    meeting_state: SkipJsonSchema[MeetingPlan | None] = Field(default=None, exclude=True)


class Conversation(Record):
    turn_count: int = 0
    turn_offset: int = 0
    history_prefix_sha256: str = ""
    history_anchor: str = ""
    notes: list[str] = Field(default_factory=list)
    source_conversation_id: UUID | None = None
    source_turn_id: UUID | None = None
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


class TurnRequest(Record):
    """Read immutable accepted requests independently of the public input contract."""

    operation_id: UUID
    text: str = Field(default="", max_length=20000)
    action_id: UUID | None = None
    action_origin: Literal["explicit", "conversation"] | None = None
    expected_revision: int | None = None
    action: PlayerAction | None = None
    proposal_id: UUID | None = None
    allow_real_llm: bool = False
    locale: Literal["ja", "en"] | None = None
    meeting_state: MeetingPlan | None = None


class SendInput(Record):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    text: str = Field(default="", max_length=20000)
    action: PlayerAction | None = None
    proposal_id: UUID | None = None
    allow_real_llm: bool = False
    locale: Literal["ja", "en"] | None = None

    @model_validator(mode="after")
    def content_required(self) -> "SendInput":
        if not self.text.strip() and self.action is None:
            raise ValueError("A message or an explicit action is required.")
        if self.proposal_id is not None and self.action is None:
            raise ValueError("A proposal requires its matching action.")
        return self


class Reply(Record):
    action_narration: str = Field(default="", max_length=20000)
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
    meeting_change: MeetingChange | None = None
    meeting_query: str | None = None


type TurnResult = Reply | InferredMove
TURN_RESULT = TypeAdapter(cast("type[TurnResult]", TurnResult))


class AcceptedInput(Record):
    text: str = Field(max_length=20000)
    action: PlayerAction | None = None


class Operation(Record):
    resume_count: int = Field(default=0, ge=0)
    result_revision: int | None = Field(default=None, ge=0)
    can_resume: bool = False
    nsfw_enabled: bool = False
    id: UUID
    conversation_id: UUID
    state: Literal["accepted", "running", "complete", "failed", "interrupted"]
    error: str | None = None
    error_stage: OperationStage | None = None
    response_locale: Literal["ja", "en"] | None = None
    image_state: Literal["disabled", "pending", "submitted", "failed"] = "disabled"
    image_id: UUID | None = None
    image_error: str | None = None
    created_at: datetime = Field(default_factory=now)
