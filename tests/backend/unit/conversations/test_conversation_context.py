"""Verify conversations contracts within the declared test scope."""

from uuid import uuid4

import pytest
from komaquill.domain.conversations.context import drawing_context, scene_context
from komaquill.domain.conversations.models import Conversation, ConversationInput, Turn
from komaquill.domain.roleplay import ConversationState, StatePatch


def test_prose_never_overrides_current_people_or_location():
    state = ConversationState(
        place="sea",
        character_place="sea",
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
    assert scene_context(conversation)["current_state"] == state.model_dump(
        exclude={"player", "character"}
    )
    drawing = drawing_context(conversation)
    assert drawing["character"]["clothing"] == "black apron"
    assert "current_character" not in drawing
    assert drawing["setting"]["place"] == "sea"
    assert drawing["character"]["age"] == "60"
    assert "red coat" not in str(drawing)


def test_patch_retains_clears_and_transfers_ownership_without_mutating_previous():
    previous = ConversationState(
        place="shop",
        player={"held_objects": [{"id": "coat", "name": "red coat"}]},
        character={"clothing": "black apron", "pose": "standing"},
    )
    updated = StatePatch.model_validate(
        {
            "place": "sea",
            "player": {"held_objects": []},
            "character": {"held_objects": [{"id": "coat", "name": "red coat"}], "pose": None},
        }
    ).apply(previous)
    assert updated.place == "sea" and updated.character.clothing == "black apron"
    assert [item.model_dump() for item in updated.character.held_objects] == [
        {"id": "coat", "name": "red coat"}
    ] and not updated.player.held_objects
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
        character={"clothing": "black apron", "held_objects": [{"id": "shell", "name": "shell"}]},
    )
    desired = ConversationState(
        place="sea", character={"clothing": "blue shirt", "pose": "sitting", "held_objects": []}
    )
    patch = StatePatch.between(previous, desired)
    updated = patch.apply(previous)
    assert updated.place == "sea" and updated.player.clothing == "red coat"
    assert updated.character.clothing == "blue shirt" and updated.character.held_objects == []
    assert previous.character.clothing == "black apron"


def test_wire_changes_exclude_player_fields_and_preserve_canonical_limits():
    from komaquill.adapters.llm.conversation import (
        DialogueResult,
        ExpressionPoseChange,
        MovementChange,
        NarrativeReply,
        ObjectsChange,
        resolve_scene,
    )

    for model in (
        DialogueResult,
        ExpressionPoseChange,
        MovementChange,
        ObjectsChange,
        NarrativeReply,
    ):
        schema = model.model_json_schema()
        assert set(schema["required"]) == set(schema["properties"])
    assert set(NarrativeReply.model_fields) == {"response"}
    with pytest.raises(ValueError):
        ExpressionPoseChange(field="place", value="Another place")
    with pytest.raises(ValueError):
        resolve_scene(
            Conversation(definition=ConversationInput(title="Limits")),
            DialogueResult(
                kind="companion",
                changes=[ExpressionPoseChange(field="pose", value="x" * 1001)],
                action_narration="",
                text="Hello",
            ),
        )


def test_history_projection_preserves_repetitions_and_distinct_state_boundaries():
    import json
    from types import SimpleNamespace

    from komaquill.adapters.llm.context import ContextBudget
    from komaquill.adapters.llm.memory import history_messages
    from komaquill.domain.conversations.models import SendInput

    state = ConversationState(place="Bookshop")
    rows = [
        Turn(operation_id=uuid4(), user="Again", assistant="Yes", state=state),
        Turn(operation_id=uuid4(), user="Again", assistant="Yes", state=state),
        Turn(
            operation_id=uuid4(),
            user="Again",
            assistant="Yes",
            state=state,
            notes=["Keep the key."],
        ),
        Turn(
            operation_id=uuid4(),
            user="Again",
            assistant="Yes",
            state=state.model_copy(update={"place": "Garden"}),
        ),
    ]
    conversation = Conversation(
        definition=ConversationInput(title="Repeated turns"), turns=rows, turn_count=len(rows)
    )
    responder = SimpleNamespace(
        settings=SimpleNamespace(recent_turns=1, memory_max_batches=1),
        budget=lambda *_: ContextBudget(32768, 32768, 512, 128, "{}"),
        system_text=lambda _, value: value,
    )
    messages, memory = history_messages(
        responder,
        conversation,
        SendInput(expected_revision=0, operation_id=uuid4(), text="Continue"),
        lambda _: "Instructions",
        "Continue",
        "{}",
        lambda _: "",
    )
    projected = json.loads(messages[0][1])["history"]
    assert [(p["turn"], p["consecutive_turns"]) for p in projected] == [(0, 2), (2, 1), (3, 1)]
    assert all(p["user"] == "Again" and p["assistant"] == "Yes" for p in projected)
    assert memory is None and conversation.turns == rows
