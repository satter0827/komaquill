"""Exercise confirmed actions and recovery against the real SQLite repository."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.api import Client, ConversationInput, KomaquillError, SendInput
from komaquill.domain.conversations.actions import (
    MoveAction,
    PrepareAction,
    ProposalDismissal,
    WaitAction,
)
from komaquill.domain.conversations.models import Reply
from komaquill.domain.roleplay import StatePatch

from tests.backend.support.conversations import send_action


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
    request = SendInput(
        text="Continue.",
        operation_id=uuid4(),
        expected_revision=0,
        action=MoveAction(destination="Beach"),
    )
    record = send_action(service, conversation.id, request)
    assert client.operations.get(record.reaction_id).error == "llm_unavailable"
    resumed = Client(client.workspace).conversations.get(conversation.id)
    assert resumed.state.place == "Beach"
    assert resumed.state.model_dump().get("character_place") is None
    assert resumed.revision == 1
    assert send_action(service, conversation.id, request) == record
    assert len(service.actions(conversation.id)) == 1
    with pytest.raises(KomaquillError, match="operation_conflict"):
        send_action(
            service,
            conversation.id,
            request.model_copy(update={"action": MoveAction(destination="Forest")}),
        )


def test_nonmovement_action_remains_a_proposal_until_confirmed(story):
    _, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="",
        state_patch=StatePatch(),
        action_proposals=[PrepareAction(clothing="Blue coat")],
    )
    service.send(
        conversation.id,
        SendInput(
            expected_revision=service.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="I put on a blue coat.",
        ),
    )
    pending = service.get(conversation.id)
    assert pending.state.place == "Shop"
    proposal = pending.proposals[0]
    service.responder.reply = lambda *_: Reply(
        text="Ready.", narration="The sea is quiet.", state_patch=StatePatch()
    )
    send_action(
        service,
        conversation.id,
        SendInput(
            text="Continue.",
            operation_id=uuid4(),
            expected_revision=pending.revision,
            proposal_id=proposal.id,
            action=proposal.action,
        ),
    )
    result = service.get(conversation.id)
    assert result.state.place == "Shop"
    assert result.state.player.clothing == "Blue coat"
    assert result.proposals[0].state == "applied"
    assert result.turns[-1].action_id == result.actions[0].request.operation_id


def test_reply_cannot_apply_player_changes(story):
    client, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="Here.", state_patch=StatePatch(player={"clothing": "coat"})
    )
    operation = service.send(
        conversation.id,
        SendInput(
            expected_revision=service.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Hello",
        ),
    )
    assert operation.state == "failed"
    assert operation.error == "invalid_response"
    assert client.conversations.get(conversation.id).revision == 0


def test_published_background_uses_the_same_facts_as_drawing(story):
    from komaquill.domain.conversations.context import drawing_context

    _, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="Hello",
        narration="You walk away wearing someone else's coat.",
        state_patch=StatePatch(scenery="Rain outside the shop."),
    )
    service.send(
        conversation.id,
        SendInput(
            expected_revision=service.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Hello",
        ),
    )
    result = service.get(conversation.id)
    assert result.turns[-1].narration == drawing_context(result)["setting"]["scenery"]
    assert result.turns[-1].narration == "Rain outside the shop."


def test_unknown_generated_properties_do_not_restore_removed_clothes(story):
    from komaquill.adapters.llm.conversation import DialogueResult, resolve_scene
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
    generated = DialogueResult(
        kind="companion",
        meeting_change=None,
        meeting_query=None,
        changes=[],
        action_narration="",
        text="Hello",
    )
    result = resolve_scene(current, generated)
    assert result.character.clothing == "" and result.character.held_objects == []


def test_stale_proposal_and_dismissal(story):
    _, service, conversation = story
    service.responder.reply = lambda *_: Reply(
        text="", state_patch=StatePatch(), action_proposals=[WaitAction(minutes=5)]
    )
    service.send(
        conversation.id,
        SendInput(
            expected_revision=service.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Wait",
        ),
    )
    current = service.get(conversation.id)
    proposal = current.proposals[0]
    dismissal = ProposalDismissal(operation_id=uuid4(), expected_revision=current.revision)
    service.dismiss_proposal(conversation.id, proposal.id, dismissal)
    assert (
        service.dismiss_proposal(conversation.id, proposal.id, dismissal).proposals[0].state
        == "dismissed"
    )
    with pytest.raises(KomaquillError):
        send_action(
            service,
            conversation.id,
            SendInput(
                text="Continue.",
                operation_id=uuid4(),
                expected_revision=current.revision,
                proposal_id=proposal.id,
                action=proposal.action,
            ),
        )


def test_explicit_removal_and_wait_keep_fixed_identity(story):
    _, service, conversation = story
    record = send_action(
        service,
        conversation.id,
        SendInput(
            text="Continue.",
            operation_id=uuid4(),
            expected_revision=0,
            action=PrepareAction(clothing="", held_objects=[]),
        ),
    )
    assert record.after.player.clothing == ""
    assert record.after.player.held_objects == []
    current = service.get(conversation.id)
    send_action(
        service,
        conversation.id,
        SendInput(
            text="Continue.",
            operation_id=uuid4(),
            expected_revision=current.revision,
            action=WaitAction(minutes=15),
        ),
    )
    assert service.get(conversation.id).state.elapsed_minutes == 15


def test_operation_ids_cannot_be_reused_in_another_conversation(story):
    _, service, conversation = story
    request = SendInput(
        text="Continue.", operation_id=uuid4(), expected_revision=0, action=WaitAction(minutes=1)
    )
    send_action(service, conversation.id, request)
    other = service.create(ConversationInput(title="Other", llm_profile="fake"))
    with pytest.raises(KomaquillError, match="operation_conflict"):
        send_action(service, other.id, request)
    with pytest.raises(KomaquillError, match="operation_conflict"):
        service.send(
            other.id,
            SendInput(
                expected_revision=service.overview(other.id).revision,
                operation_id=request.operation_id,
                text="Hello",
            ),
        )


def test_retained_appearance_allows_actions_without_rewriting_review_history(story):
    from komaquill.adapters.persistence.schema import conversation_events
    from komaquill.domain.conversations.context import drawing_context
    from sqlalchemy import select

    client, service, _ = story
    original = service.create(
        ConversationInput(
            title="Retained appearance",
            llm_profile="fake",
            character={
                "appearance": "Silver hair and an old apron",
                "visual_review_required": True,
            },
        )
    )
    event_id = uuid4()
    raw = (
        '{ "request": { "operation_id": "'
        + str(event_id)
        + '", "expected_revision": 0, "character": { "appearance": "Silver hair", '
        '"initial_clothing": null, "current_clothing": null } }, '
        '"revision": 1, "created_at": "2026-11-01T00:00:00Z" }'
    )
    with service.repository.database.engine.begin() as connection:
        connection.execute(
            conversation_events.insert().values(
                conversation_id=str(original.id),
                kind="visual_reviews",
                id=str(event_id),
                payload=raw,
                pending=0,
            )
        )
    snapshot = drawing_context(service.get(original.id))
    assert snapshot["character"]["appearance_reference"] == original.definition.character.appearance
    operation, created = service.accept(
        original.id,
        SendInput(operation_id=uuid4(), expected_revision=0, action=MoveAction(destination="Park")),
    )
    assert created and operation.state == "accepted"
    updated = Client(client.workspace).conversations.get(original.id)
    assert updated.state.place == "Park"
    assert updated.definition == original.definition
    assert updated.visual_reviews[0].request.operation_id == event_id
    with service.repository.database.engine.connect() as connection:
        assert (
            connection.execute(
                select(conversation_events.c.payload).where(
                    conversation_events.c.id == str(event_id)
                )
            ).scalar_one()
            == raw
        )


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
        SendInput(text="Continue.", operation_id=uuid4(), expected_revision=0, action=data)
