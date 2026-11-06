"""Define explicit changes, saved scenes and branch requests."""

from typing import Annotated
from uuid import UUID

from pydantic import Field

from komaquill.domain.roleplay import ConversationState, Record

MAX_PINNED_NOTES = 32
MAX_NOTE_CHARACTERS = 1000
PinnedNote = Annotated[str, Field(min_length=1, max_length=MAX_NOTE_CHARACTERS)]


class NotesRequest(Record):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    notes: list[PinnedNote] = Field(max_length=MAX_PINNED_NOTES)


class SceneRequest(Record):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    turn_id: UUID
    name: str = Field(min_length=1, max_length=120)


class Bookmark(Record):
    id: UUID
    conversation_id: UUID
    turn_id: UUID
    name: str
    state: ConversationState
    notes: list[str]
