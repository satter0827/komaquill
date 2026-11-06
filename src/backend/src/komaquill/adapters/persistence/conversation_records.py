"""Read retained state snapshots without changing their source bytes or history hashes."""

import json
from typing import Any

from pydantic import Field

from komaquill.domain.conversations.actions import ActionRecord
from komaquill.domain.conversations.editing import Bookmark
from komaquill.domain.conversations.models import Conversation, Turn
from komaquill.domain.roleplay import MAX_PLACE_LENGTH, ConversationState


class RecordedState(ConversationState):
    """Keep the retired location readable only at the retained-record boundary."""

    character_place: str = Field(default="", max_length=MAX_PLACE_LENGTH, exclude=True)


def state(value: object) -> dict[str, Any]:
    return RecordedState.model_validate(value).model_dump(mode="json")


def read_turn(value: dict[str, Any]) -> Turn:
    return Turn.model_validate({**value, "state": state(value["state"])})


def read_conversation(payload: str) -> Conversation:
    value = json.loads(payload)
    definition = value["definition"]
    return Conversation.model_validate(
        {
            **value,
            "state": state(value["state"]),
            "definition": {
                **definition,
                "initial_state": state(definition.get("initial_state", {})),
            },
            "turns": [read_turn(turn) for turn in value.get("turns", [])],
            "actions": [read_action(json.dumps(action)) for action in value.get("actions", [])],
        }
    )


def read_action(payload: str) -> ActionRecord:
    value = json.loads(payload)
    return ActionRecord.model_validate(
        {**value, "before": state(value["before"]), "after": state(value["after"])}
    )


def read_bookmark(payload: str) -> Bookmark:
    value = json.loads(payload)
    return Bookmark.model_validate({**value, "state": state(value["state"])})
