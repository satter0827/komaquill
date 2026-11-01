"""Preserve current facts independently of narration and apply explicit changes."""

from uuid import uuid4

import pytest
from komaquill.conversations.context import drawing_context, scene_context
from komaquill.conversations.models import Conversation, ConversationInput, Turn
from komaquill.domain.roleplay import ConversationState, StatePatch


def test_prose_never_overrides_current_people_or_location():
    state = ConversationState(
        place="sea",
        player={"clothing": "red coat"},
        character={"clothing": "black apron", "pose": "standing"},
    )
    conversation = Conversation(
        definition=ConversationInput(
            title="Scene",
            character={"name": "Mira", "age": "60", "gender": "man"},
            user={"name": "Traveler", "appearance": "red coat"},
        ),
        state=state,
        turns=[
            Turn(
                operation_id=uuid4(),
                user="Go to the sea",
                assistant="Welcome",
                narration="A bookshop",
                state=state,
            )
        ],
    )
    assert scene_context(conversation)["current_state"] == state.model_dump()
    drawing = drawing_context(conversation)
    assert drawing["current_character"] == state.character.model_dump()
    assert drawing["setting"]["place"] == "sea"
    assert drawing["character"]["age"] == "60"
    assert "red coat" not in str(drawing)


def test_patch_retains_clears_and_transfers_ownership_without_mutating_previous():
    previous = ConversationState(
        place="shop",
        player={"held_objects": ["red coat"]},
        character={"clothing": "black apron", "pose": "standing"},
    )
    updated = StatePatch.model_validate(
        {
            "place": "sea",
            "player": {"held_objects": []},
            "character": {"held_objects": ["red coat"], "pose": None},
        }
    ).apply(previous)
    assert updated.place == "sea" and updated.character.clothing == "black apron"
    assert updated.character.held_objects == ["red coat"] and not updated.player.held_objects
    assert updated.character.pose == "" and previous.character.pose == "standing"
    assert StatePatch().apply(previous) == previous
    assert StatePatch(character=None, place=None).apply(previous).character.clothing is None
    assert StatePatch(place=None).apply(previous).place == ""


@pytest.mark.parametrize(
    "data",
    [
        {"character": {"age": "4"}},
        {"character_clothing": "coat"},
        {"player": {"held_objects": "coat"}},
    ],
)
def test_state_patch_rejects_identity_changes_and_untyped_keys(data):
    with pytest.raises(ValueError):
        StatePatch.model_validate(data)


def test_generated_snapshot_retains_unknown_values_and_applies_known_changes():
    previous = ConversationState(
        place="shop",
        player={"clothing": "red coat"},
        character={"clothing": "black apron", "held_objects": ["shell"]},
    )
    desired = ConversationState(
        place="sea", character={"clothing": "blue shirt", "pose": "sitting", "held_objects": []}
    )
    patch = StatePatch.between(previous, desired)
    updated = patch.apply(previous)
    assert updated.place == "sea" and updated.player.clothing == "red coat"
    assert updated.character.clothing == "blue shirt" and updated.character.held_objects == []
    assert previous.character.clothing == "black apron"


def test_wire_snapshot_requires_every_field_and_preserves_canonical_limits():
    from komaquill.adapters.llm.conversation import (
        GeneratedPersonState,
        GeneratedState,
        NarrativeReply,
    )

    for model in (GeneratedState, GeneratedPersonState, NarrativeReply):
        schema = model.model_json_schema()
        assert set(schema["required"]) == set(schema["properties"])
    assert next(iter(NarrativeReply.model_fields)) == "current_state"
    with pytest.raises(ValueError):
        GeneratedState.model_validate({**ConversationState().model_dump(), "place": "x" * 1001})
