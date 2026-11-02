"""Exercise confirmed actions and recovery against the real SQLite repository."""

from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from komaquill.api import Client, ConversationInput, KomaquillError, SendInput
from komaquill.conversations.actions import (
    ActionRequest,
    MoveAction,
    PrepareAction,
    ProposalDismissal,
    WaitAction,
)
from komaquill.conversations.models import Reply
from komaquill.domain.roleplay import StatePatch


@pytest.fixture
def story(tmp_path):
    client = Client.initialize(tmp_path)
    service = client.conversations
    conversation = service.create(
        ConversationInput(title="A walk", llm_profile="fake", initial_state={"place": "Shop"})
    )
    service.responder = SimpleNamespace(
        profiles=service.responder.profiles,
        reply=lambda *_: Reply(text="", narration="The sea is quiet.", state_patch=StatePatch()),
    )
    return client, service, conversation


def test_action_survives_failed_reaction_and_is_not_reapplied(story):
    client, service, conversation = story
    service.responder.reply = lambda *_: (_ for _ in ()).throw(
        KomaquillError(code="llm_unavailable")
    )
    request = ActionRequest(
        operation_id=uuid4(), expected_revision=0, action=MoveAction(destination="Beach")
    )
    record = service.act(conversation.id, request)
    assert client.operations.get(record.reaction_id).error == "llm_unavailable"
    resumed = Client(client.workspace).conversations.get(conversation.id)
    assert resumed.state.place == "Beach"
    assert resumed.state.character_place == "Shop"
    assert resumed.revision == 1
    assert service.act(conversation.id, request) == record
    assert len(service.actions(conversation.id)) == 1
    with pytest.raises(KomaquillError, match="operation_conflict"):
        service.act(
            conversation.id, request.model_copy(update={"action": MoveAction(destination="Forest")})
        )


def test_chat_proposes_then_player_confirms(story):
    _, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="Shall we go?",
        narration="A quiet shop.",
        state_patch=StatePatch(),
        action_proposals=[MoveAction(destination="Beach")],
    )
    service.send(conversation.id, SendInput(operation_id=uuid4(), text="I head to the beach."))
    pending = service.get(conversation.id)
    assert pending.state.place == "Shop"
    proposal = pending.proposals[0]
    service.responder.reply = lambda *_: Reply(
        text="", narration="The sea is quiet.", state_patch=StatePatch()
    )
    service.act(
        conversation.id,
        ActionRequest(
            operation_id=uuid4(),
            expected_revision=pending.revision,
            proposal_id=proposal.id,
            action=proposal.action,
        ),
    )
    result = service.get(conversation.id)
    assert result.state.place == "Beach"
    assert result.proposals[0].state == "applied"
    assert result.turns[-1].action_id == result.actions[0].request.operation_id


def test_reply_cannot_apply_player_changes(story):
    client, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="Here.", state_patch=StatePatch(player={"clothing": "coat"})
    )
    operation = service.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
    assert operation.state == "failed"
    assert operation.error == "invalid_response"
    assert client.conversations.get(conversation.id).revision == 0


def test_published_background_uses_the_same_facts_as_drawing(story):
    from komaquill.conversations.context import drawing_context

    _, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="Hello",
        narration="You walk away wearing someone else's coat.",
        state_patch=StatePatch(scenery="Rain outside the shop."),
    )
    service.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
    result = service.get(conversation.id)
    assert result.turns[-1].narration == drawing_context(result)["setting"]["scenery"]
    assert result.turns[-1].narration == "Rain outside the shop."


def test_unknown_generated_properties_do_not_restore_removed_clothes(story):
    from komaquill.adapters.llm.conversation import GeneratedState, resolve_scene
    from komaquill.domain.roleplay import PersonState

    _, _, conversation = story
    current = conversation.model_copy(
        update={
            "state": conversation.state.model_copy(
                update={
                    "character": PersonState(clothing="", held_objects=[]),
                }
            )
        }
    )
    generated = GeneratedState(
        relationship="",
        companion_movement="stay",
        scenery="",
        character={"clothing": None, "pose": None, "expression": None, "held_objects": None},
    )
    result = resolve_scene(current, generated)
    assert result.character.clothing == "" and result.character.held_objects == []


def test_stale_proposal_and_dismissal(story):
    _, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="Hello", state_patch=StatePatch(), action_proposals=[WaitAction(minutes=5)]
    )
    service.send(conversation.id, SendInput(operation_id=uuid4(), text="Wait"))
    current = service.get(conversation.id)
    proposal = current.proposals[0]
    dismissal = ProposalDismissal(operation_id=uuid4(), expected_revision=current.revision)
    service.dismiss_proposal(conversation.id, proposal.id, dismissal)
    assert (
        service.dismiss_proposal(conversation.id, proposal.id, dismissal).proposals[0].state
        == "dismissed"
    )
    with pytest.raises(KomaquillError):
        service.act(
            conversation.id,
            ActionRequest(
                operation_id=uuid4(),
                expected_revision=current.revision,
                proposal_id=proposal.id,
                action=proposal.action,
            ),
        )


def test_explicit_removal_and_wait_keep_fixed_identity(story):
    _, service, conversation = story
    record = service.act(
        conversation.id,
        ActionRequest(
            operation_id=uuid4(),
            expected_revision=0,
            action=PrepareAction(clothing="", held_objects=[]),
        ),
    )
    assert record.after.player.clothing == ""
    assert record.after.player.held_objects == []
    current = service.get(conversation.id)
    service.act(
        conversation.id,
        ActionRequest(
            operation_id=uuid4(), expected_revision=current.revision, action=WaitAction(minutes=15)
        ),
    )
    assert service.get(conversation.id).state.elapsed_minutes == 15


def test_operation_ids_cannot_be_reused_in_another_conversation(story):
    _, service, conversation = story
    request = ActionRequest(operation_id=uuid4(), expected_revision=0, action=WaitAction(minutes=1))
    service.act(conversation.id, request)
    other = service.create(ConversationInput(title="Other", llm_profile="fake"))
    with pytest.raises(KomaquillError, match="operation_conflict"):
        service.act(other.id, request)
    with pytest.raises(KomaquillError, match="operation_conflict"):
        service.send(other.id, SendInput(operation_id=request.operation_id, text="Hello"))


def test_derived_reaction_id_cannot_reuse_a_saved_decision_id(story):
    _, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="", state_patch=StatePatch(), action_proposals=[WaitAction(minutes=5)]
    )
    service.send(conversation.id, SendInput(operation_id=uuid4(), text="I wait"))
    pending = service.get(conversation.id)
    action_id = uuid4()
    service.dismiss_proposal(
        conversation.id,
        pending.proposals[0].id,
        ProposalDismissal(
            operation_id=uuid5(action_id, "reaction"), expected_revision=pending.revision
        ),
    )
    with pytest.raises(KomaquillError, match="operation_conflict"):
        service.act(
            conversation.id,
            ActionRequest(
                operation_id=action_id,
                expected_revision=pending.revision,
                action=WaitAction(minutes=1),
            ),
        )
    assert service.get(conversation.id).actions == []


def test_visual_review_is_explicit_idempotent_and_used_by_drawing(story):
    from komaquill.adapters.database import conversations
    from komaquill.conversations.context import drawing_context
    from komaquill.conversations.visuals import VisualReview

    client, service, conversation = story
    definition = conversation.definition.model_copy(
        update={
            "character": conversation.definition.character.model_copy(
                update={
                    "appearance": "Silver hair and an old apron",
                    "visual_review_required": True,
                }
            )
        }
    )
    old = conversation.model_copy(update={"definition": definition})
    with service.repository.database.engine.begin() as connection:
        connection.execute(
            conversations.update()
            .where(conversations.c.id == str(old.id))
            .values(payload=old.model_dump_json())
        )
    with pytest.raises(KomaquillError, match="visual_review_required"):
        drawing_context(service.get(old.id))
    review = VisualReview(
        operation_id=uuid4(),
        expected_revision=0,
        character={
            "appearance": "Silver hair",
            "initial_clothing": "Black apron",
            "current_clothing": "Green coat",
        },
    )
    current = service.review_visuals(old.id, review)
    assert service.review_visuals(old.id, review) == current
    snapshot = drawing_context(current)
    assert snapshot["character"]["physical_features"] == "Silver hair"
    assert snapshot["character"]["clothing"] == "Green coat"
    assert "old apron" not in str(snapshot)
    assert current.definition.initial_state.character.clothing == "Black apron"
    assert len(Client(client.workspace).conversations.get(old.id).visual_reviews) == 1
    with pytest.raises(KomaquillError, match="operation_conflict"):
        service.review_visuals(old.id, review.model_copy(update={"expected_revision": 1}))


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "prepare"},
        {"kind": "prepare", "clothing": None},
        {"kind": "wait", "minutes": 0},
        {"kind": "move", "destination": " "},
    ],
)
def test_invalid_actions_are_rejected_before_persistence(data):
    with pytest.raises(ValueError):
        ActionRequest(operation_id=uuid4(), expected_revision=0, action=data)
