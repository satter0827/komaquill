"""Validate explicit player actions without interpreting natural language."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import (
    ConversationState,
    PersonStatePatch,
    PhysicalObjects,
    Record,
    StatePatch,
)

MAX_WAIT_MINUTES = 60 * 24 * 30


class MoveAction(Record):
    kind: Literal["move"] = "move"
    destination: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class WaitAction(Record):
    kind: Literal["wait"] = "wait"
    minutes: int = Field(gt=0, le=MAX_WAIT_MINUTES)


class PrepareAction(Record):
    kind: Literal["prepare"] = "prepare"
    clothing: str | None = Field(default=None, max_length=3000)
    pose: str | None = Field(default=None, max_length=1000)
    expression: str | None = Field(default=None, max_length=1000)
    held_objects: PhysicalObjects | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def has_change(self) -> "PrepareAction":
        if not any(value is not None for key, value in self.model_dump().items() if key != "kind"):
            raise ValueError("An explicit change is required.")
        return self


class IntentionAction(Record):
    kind: Literal["intention"] = "intention"
    text: str = Field(max_length=2000)


PlayerAction = Annotated[
    MoveAction | WaitAction | PrepareAction | IntentionAction, Field(discriminator="kind")
]


class ProposedAction(Record):
    id: UUID
    turn_id: UUID
    revision: int
    action: PlayerAction
    state: Literal["pending", "applied", "dismissed", "expired"] = "pending"
    decision_id: UUID | None = None


class ActionRequest(Record):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    action: PlayerAction
    proposal_id: UUID | None = None
    locale: Literal["ja", "en"] | None = None
    allow_real_llm: bool = False


class ActionRecord(Record):
    request: ActionRequest
    before: ConversationState
    after: ConversationState
    revision: int
    reaction_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProposalDismissal(Record):
    operation_id: UUID
    expected_revision: int = Field(ge=0)


def apply_action(state: ConversationState, action: PlayerAction) -> ConversationState:
    if isinstance(action, MoveAction):
        return state.model_copy(update={"place": action.destination, "scenery": ""})
    if isinstance(action, WaitAction):
        return state.model_copy(update={"elapsed_minutes": state.elapsed_minutes + action.minutes})
    if isinstance(action, IntentionAction):
        return state.model_copy(update={"intention": action.text})
    changes = action.model_dump(exclude_none=True, exclude={"kind"})
    objects = changes.get("held_objects") or []
    if {item["id"] for item in objects}.intersection(
        item.id for item in state.character.held_objects or []
    ):
        raise KomaquillError(code="invalid_request", retryable=False)
    return StatePatch(player=PersonStatePatch.model_validate(changes)).apply(state)
