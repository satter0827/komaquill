"""Reject contradictory speech without losing the original model response."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client, ConversationInput, SendInput
from komaquill.domain.conversations.actions import MoveAction
from komaquill.domain.conversations.models import Reply
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

pytestmark = pytest.mark.usefixtures("scripted_services")


def model_for(api, responses):
    name = api._execution.settings.llm_profiles["lmstudio-4b"].model
    return FakeMessagesListChatModel(
        responses=[
            AIMessage(content=json.dumps(response), response_metadata={"model_name": name})
            for response in responses
        ]
    )


@pytest.mark.parametrize("corrected", [True, False])
def test_absent_companion_is_repaired_or_failed_instead_of_silenced(
    tmp_path, monkeypatch, corrected
):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(title="Presence", locale="en", initial_state={"place": "Shop"})
    )
    invalid = {
        "response": {
            "kind": "companion",
            "movement": "stay",
            "changes": [],
            "action_narration": "Waves hello.",
            "text": "Hello at the beach.",
        }
    }
    valid = {"response": {**invalid["response"], "movement": "join_player"}}
    model = model_for(api, [invalid, valid if corrected else invalid])
    monkeypatch.setattr(api._execution, "_model", lambda _: model)
    result = api.conversations.send(
        scene.id,
        SendInput(
            text="Join me.",
            locale="en",
            allow_real_llm=True,
            operation_id=uuid4(),
            expected_revision=0,
            action=MoveAction(destination="Beach"),
        ),
    )
    records = api.llm_records(result.id)
    assert len(records) == 2
    assert json.loads(records[0]["response"]) == invalid
    saved = api.conversations.get(scene.id)
    assert saved.state.place == "Beach" and len(saved.actions) == 1
    if corrected:
        assert result.state == "complete"
        assert saved.turns[-1].assistant == "Hello at the beach."
        assert saved.turns[-1].action_narration == "Waves hello."
        assert saved.state.character_place == "Beach"
    else:
        assert result.state == "failed" and result.error == "invalid_response"
        assert saved.turn_count == 0 and saved.state.character_place == "Shop"
    api.close()


def test_explicit_stay_preserves_the_companion_place_without_speech(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(title="Stay", locale="en", initial_state={"place": "Shop"})
    )
    reply = {
        "response": {
            "kind": "companion",
            "movement": "stay",
            "changes": [{"field": "scenery", "value": "An empty beach."}],
            "action_narration": "",
            "text": "",
        }
    }
    monkeypatch.setattr(api._execution, "_model", lambda _: model_for(api, [reply]))
    operation = api.conversations.send(
        scene.id,
        SendInput(
            text="Stay here while I go.",
            locale="en",
            allow_real_llm=True,
            expected_revision=0,
            operation_id=uuid4(),
            action=MoveAction(destination="Beach"),
        ),
    )
    assert operation.state == "complete"
    saved = api.conversations.get(scene.id)
    assert saved.state.character_place == "Shop" and saved.turns[-1].assistant == ""
    assert saved.turns[-1].narration == "An empty beach."
    api.close()


def test_private_state_change_cannot_complete_an_empty_reply(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(title="Visible response", locale="en", llm_profile="fake")
    )
    monkeypatch.setattr(
        api.conversations.responder,
        "reply",
        lambda *_: Reply(text="", state_patch={"character": {"pose": "standing"}}),
    )
    result = api.conversations.send(
        scene.id, SendInput(text="Hello", expected_revision=0, operation_id=uuid4())
    )
    assert result.state == "failed" and result.error == "invalid_response"
    assert api.conversations.get(scene.id).turn_count == 0
    assert api.conversations.get(scene.id).state == scene.state
    api.close()


@pytest.mark.parametrize("with_action", [True, False])
@pytest.mark.parametrize("corrected", [True, False])
def test_present_companion_must_produce_visible_content_even_after_an_action(
    tmp_path, monkeypatch, with_action, corrected
):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(title="Empty reply", locale="en", initial_state={"place": "Shop"})
    )
    invalid = {
        "response": {
            "kind": "companion",
            "movement": "join_player",
            "changes": [{"field": "pose", "value": "standing"}],
            "text": "",
            "action_narration": "",
        }
    }
    valid = {"response": {**invalid["response"], "text": "I am here."}}
    monkeypatch.setattr(
        api._execution,
        "_model",
        lambda _: model_for(api, [invalid, valid if corrected else invalid]),
    )
    result = api.conversations.send(
        scene.id,
        SendInput(
            text="Continue.",
            expected_revision=0,
            operation_id=uuid4(),
            allow_real_llm=True,
            action=MoveAction(destination="Beach") if with_action else None,
        ),
    )
    assert len(api.llm_records(result.id)) == 2
    assert result.state == ("complete" if corrected else "failed")
    saved = api.conversations.get(scene.id)
    assert saved.turn_count == int(corrected)
    if corrected:
        assert saved.turns[-1].assistant == "I am here."
    else:
        assert result.error == "invalid_response"
    api.close()


def test_new_visible_scenery_is_a_valid_silent_response(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(ConversationInput(title="Observe", locale="en"))
    response = {
        "response": {
            "kind": "companion",
            "movement": "stay",
            "changes": [{"field": "scenery", "value": "Sunlight fills the room."}],
            "text": "",
            "action_narration": "",
        }
    }
    monkeypatch.setattr(api._execution, "_model", lambda _: model_for(api, [response]))
    result = api.conversations.send(
        scene.id,
        SendInput(
            text="Describe the room.",
            expected_revision=0,
            operation_id=uuid4(),
            allow_real_llm=True,
        ),
    )
    assert result.state == "complete" and len(api.llm_records(result.id)) == 1
    assert api.conversations.get(scene.id).turns[-1].narration == "Sunlight fills the room."
    api.close()
