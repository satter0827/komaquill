"""Move together and finish the spoken reply under one durable operation."""

import json
from contextlib import closing
from uuid import uuid4

import pytest
from komaquill.api import Client, ConversationInput, KomaquillError, MoveAction, SendInput
from komaquill.domain.conversations.actions import InferredMove
from komaquill.domain.conversations.evidence import SourceSpan
from komaquill.domain.conversations.models import Reply
from pydantic import ValidationError
from sqlalchemy import select


def inferred(text, clause, destination="Park"):
    return InferredMove(
        action=MoveAction(destination=destination), source=SourceSpan.locate(text, clause)
    )


@pytest.fixture
def scene(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(
            title="Together", locale="en", llm_profile="fake", initial_state={"place": "Shop"}
        )
    )
    yield api, conversation
    api.close()


def test_natural_move_and_dialogue_finish_one_turn_without_changing_the_accepted_input(scene):
    from komaquill.adapters.persistence.schema import operations

    api, conversation = scene
    calls = []

    def reply(current, request):
        calls.append((current.state.place, request.action_origin))
        if request.action_id is None:
            return inferred(request.text, "Let us go to the park")
        return Reply(text="Let us continue the reading plan here.", state_patch={})

    api.conversations.responder.reply = reply
    request = SendInput(
        operation_id=uuid4(),
        expected_revision=0,
        text="Let us go to the park and discuss our plan.",
    )
    result = api.conversations.send(conversation.id, request)
    assert result.state == "complete"
    saved = api.conversations.get(conversation.id)
    assert saved.turn_count == 1 and saved.revision == 2 and saved.state.place == "Park"
    assert saved.turns[0].user == request.text and saved.turns[0].assistant
    assert saved.turns[0].action == MoveAction(destination="Park")
    assert len(saved.actions) == 1 and saved.actions[0].origin == "conversation"
    assert calls == [("Shop", None), ("Park", "conversation")]
    with api.conversations.repository.database.engine.connect() as connection:
        original = connection.execute(
            select(operations.c.request).where(operations.c.id == str(request.operation_id))
        ).scalar_one()
    assert json.loads(original) == request.model_dump(mode="json")
    assert api.conversations.send(conversation.id, request) == result
    assert len(calls) == 2
    with Client(api.workspace) as reopened:
        assert reopened.conversations.get(conversation.id).state.place == "Park"
        assert reopened.conversations.repository.accepted_input(
            request.operation_id
        ).action == MoveAction(destination="Park")


@pytest.mark.parametrize("text", ["Let us discuss the plan at the park.", ""])
def test_explicit_move_keeps_the_message_and_needs_only_the_reply(scene, text):
    api, conversation = scene
    calls = []

    def reply(current, request):
        calls.append((current.state.place, request.text, request.action_origin))
        return Reply(text="We can discuss it here.", state_patch={})

    api.conversations.responder.reply = reply
    result = api.conversations.send(
        conversation.id,
        SendInput(
            operation_id=uuid4(),
            expected_revision=0,
            text=text,
            action=MoveAction(destination="Park"),
        ),
    )
    assert result.state == "complete" and calls == [("Park", text, "explicit")]
    saved = api.conversations.get(conversation.id)
    assert saved.turn_count == 1 and saved.turns[0].user == text and saved.turns[0].assistant


def test_failed_reply_keeps_the_inferred_move_and_never_applies_it_twice(scene):
    api, conversation = scene
    calls = []

    def reply(current, request):
        calls.append(request.action_id)
        if request.action_id is None:
            return inferred(request.text, "Go to the park")
        raise KomaquillError(code="llm_unavailable")

    api.conversations.responder.reply = reply
    request = SendInput(operation_id=uuid4(), expected_revision=0, text="Go to the park and talk.")
    result = api.conversations.send(conversation.id, request)
    assert result.state == "failed" and result.error == "llm_unavailable"
    saved = api.conversations.get(conversation.id)
    assert saved.state.place == "Park" and saved.turn_count == 0 and saved.revision == 1
    assert len(saved.actions) == 1
    assert api.conversations.send(conversation.id, request) == result and len(calls) == 2
    api.conversations.repository.apply_inferred_move(
        result.id, inferred(request.text, "Go to the park")
    )
    assert api.conversations.get(conversation.id).revision == 1
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.conversations.repository.apply_inferred_move(
            result.id, inferred(request.text, "Go to the park", "Beach")
        )


@pytest.mark.parametrize("finished", [True, False])
def test_recovery_only_publishes_the_saved_spoken_reply(scene, monkeypatch, finished):
    from komaquill.adapters.persistence.checkpoints import CONVERSATION_WORKFLOW_VERSION
    from komaquill.adapters.persistence.schema import operations
    from langgraph.checkpoint.base import empty_checkpoint

    api, conversation = scene
    request = SendInput(operation_id=uuid4(), expected_revision=0, text="Go to the park and talk.")
    api.conversations.accept(conversation.id, request)
    repository = api.conversations.repository
    assert repository.running(request.operation_id)
    if finished:
        repository.apply_inferred_move(
            request.operation_id, inferred(request.text, "Go to the park")
        )
    retained = (
        Reply(text="We can continue here.", state_patch={})
        if finished
        else inferred(request.text, "Go to the park")
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "version": CONVERSATION_WORKFLOW_VERSION,
        "result": retained.model_dump(mode="json", exclude_unset=True),
    }
    checkpoint["channel_versions"] = {"result": 1}
    with closing(repository.database.checkpoints) as checkpoints:
        checkpoints.saver.put(
            {
                "configurable": {
                    "thread_id": f"conversation:{request.operation_id}"
                    + (":move-response" if finished else ""),
                    "checkpoint_ns": "",
                }
            },
            checkpoint,
            {"source": "loop", "step": 1, "parents": {}},
            {},
        )
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
    monkeypatch.setattr(
        api.conversations.responder,
        "reply",
        lambda *_: pytest.fail("Reading recovery must not submit a model request"),
    )
    recovered = api.conversations.resume(conversation.id)
    assert recovered.turn_count == int(finished)
    assert recovered.state.place == ("Park" if finished else "Shop")
    operation = api.operations.get(request.operation_id)
    assert operation.state == ("complete" if finished else "interrupted")
    assert not recovered.proposals


def test_empty_messages_need_an_action_and_separate_locations_are_not_public_input():
    with pytest.raises(ValidationError):
        SendInput(operation_id=uuid4(), expected_revision=0, text="  ")
    with pytest.raises(ValidationError):
        ConversationInput(
            title="Invalid", initial_state={"place": "Shop", "character_place": "Park"}
        )


@pytest.mark.parametrize("field,value", [("origin", "explicit"), ("reaction_id", str(uuid4()))])
def test_inferred_action_must_belong_to_its_original_operation(scene, field, value):
    from komaquill.adapters.persistence.schema import conversation_events

    api, conversation = scene
    identity = uuid4()
    api.conversations.accept(
        conversation.id,
        SendInput(operation_id=identity, expected_revision=0, text="Go to the park."),
    )
    repository = api.conversations.repository
    assert repository.running(identity)
    repository.apply_inferred_move(identity, inferred("Go to the park.", "Go to the park."))
    with repository.database.engine.begin() as connection:
        row = connection.execute(
            select(conversation_events.c.payload).where(conversation_events.c.id == str(identity))
        ).scalar_one()
        payload = json.loads(row)
        payload[field] = value
        connection.execute(
            conversation_events.update()
            .where(conversation_events.c.id == str(identity))
            .values(payload=json.dumps(payload))
        )
    with pytest.raises(KomaquillError, match="storage_failed"):
        repository.request(identity)


@pytest.mark.parametrize(
    "failure_point",
    [
        None,
        "before_move",
        "after_move",
        "before_checkpoint",
        "after_checkpoint",
        "before_commit",
        "after_commit",
    ],
)
def test_two_model_stages_keep_distinct_checkpoints_and_call_evidence(
    tmp_path, monkeypatch, failure_point
):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from tests.backend.support.conversations import scripted_llm_availability

    scripted_llm_availability(monkeypatch)
    meeting = "We will meet on Friday at Town Hall."
    with Client.initialize(tmp_path) as api:
        conversation = api.conversations.create(
            ConversationInput(title="A walk", locale="en", initial_state={"place": "Shop"})
        )
        name = api._execution.settings.llm_profiles["lmstudio-4b"].model
        model = FakeMessagesListChatModel(
            responses=[
                AIMessage(
                    content=json.dumps(
                        {
                            "player_move": {
                                "kind": "move",
                                "destination": "Park",
                                "quote": "Go to the park and ",
                            },
                            "response": {
                                "kind": "companion",
                                "changes": [],
                                "action_narration": "",
                                "text": "",
                            },
                        }
                    ),
                    response_metadata={"model_name": name},
                ),
                AIMessage(
                    content=json.dumps(
                        {
                            "response": {
                                "kind": "companion",
                                "changes": [],
                                "action_narration": "Opens the notebook beside you.",
                                "text": "Let us plan the reading group here.",
                                "meeting_change": {
                                    "kind": "confirm",
                                    "quote": meeting,
                                    "date": "Friday",
                                    "place": "Town Hall",
                                },
                            },
                        }
                    ),
                    response_metadata={"model_name": name},
                ),
            ]
        )
        monkeypatch.setattr(api._execution, "_model", lambda _: model)
        request = SendInput(
            operation_id=uuid4(),
            expected_revision=0,
            text=f"Go to the park and plan the reading group. {meeting}",
            allow_real_llm=True,
        )
        repository = api.conversations.repository
        if failure_point is not None:
            method = (
                "apply_inferred_move"
                if "move" in failure_point
                else "checkpoint"
                if "checkpoint" in failure_point
                else "commit"
            )
            original = getattr(repository, method)

            def interrupted(*args, **kwargs):
                monkeypatch.setattr(repository, method, original)
                if failure_point.startswith("after"):
                    original(*args, **kwargs)
                raise KomaquillError(code="storage_failed", stage="storage")

            monkeypatch.setattr(repository, method, interrupted)
        try:
            result = api.conversations.send(conversation.id, request)
        except KomaquillError as error:
            assert error.code == "storage_failed"
            api.conversations.resume(conversation.id)
            result = api.operations.get(request.operation_id)
        if result.state == "interrupted":
            assert result.can_resume
            result = api.conversations.resume_operation(request.operation_id, allow_real_llm=True)
        assert result.state == "complete"
        records = api._execution.call_records(result.id)
        assert {path.stem for path, _ in records} == {"0", "move-response-0"}
        assert all(record["validation"] == "accepted" for _, record in records)
        reaction = next(record for path, record in records if path.stem == "move-response-0")
        assert (
            json.loads(reaction["messages"][-1]["content"])["message"]
            == f"plan the reading group. {meeting}"
        )
        saved = api.conversations.get(conversation.id)
        assert saved.turn_count == 1 and saved.state.place == "Park"
        assert saved.turns[0].meeting_state.place.text == "Town Hall"
        assert len(saved.actions) == 1
        assert api.conversations.send(conversation.id, request) == result
        assert len(api.llm_records(result.id)) == 2
