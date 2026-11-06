"""Reject empty visible replies without losing the original model response."""

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
