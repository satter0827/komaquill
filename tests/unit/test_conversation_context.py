"""Keep free-form Python API inputs intact while exposing structured person facts."""

from uuid import uuid4

import pytest
from komaquill.conversations.context import definition_value, scene_context
from komaquill.conversations.models import Conversation, ConversationInput, Turn


@pytest.mark.parametrize("text", ["A quiet shop", "[1, 2]", '"Mira"', "{invalid"])
def test_plain_and_nonobject_definitions_remain_unmodified(text):
    assert definition_value(text) == text


def test_scene_context_retains_initial_state_and_explicitly_removed_settings():
    conversation = Conversation(
        definition=ConversationInput(title="Scene"), state={"place": "shop"}
    )
    assert scene_context(conversation) == {
        "latest_scene": "",
        "state": {"place": "shop"},
        "scene_settings": {},
    }
    previous = Turn(
        operation_id=uuid4(), user="Hello", assistant="Welcome", state={"place": "shop"}
    )
    assert scene_context(conversation.model_copy(update={"turns": [previous], "state": {}})) == {
        "latest_scene": "",
        "state": {},
        "scene_settings": {"place": ""},
    }
