"""Validate the loopback browser fixtures without real models or GPU access."""

import pytest
from komaquill.adapters.conversation_llm import (
    CharacterDraft,
    EnvironmentDescription,
    NarrativeReply,
    StoryDraft,
)
from komaquill.library.models import PreparedImage

from tests.support.web_server import scripted_completion


@pytest.mark.parametrize(
    "schema", [NarrativeReply, CharacterDraft, StoryDraft, PreparedImage, EnvironmentDescription]
)
def test_scripted_completion_produces_known_values_for_each_request_contract(schema):
    body = {
        "model": "fixture-model",
        "messages": [{"content": '"response_language": "English"'}],
        "response_format": {"json_schema": {"schema": schema.model_json_schema()}},
    }
    result = scripted_completion(body)
    assert result["model"] == "fixture-model"
    value = schema.model_validate_json(result["choices"][0]["message"]["content"])
    assert result["usage"]["total_tokens"] == 2
    if schema is StoryDraft:
        assert value.character_id is None
    if schema is NarrativeReply:
        assert value.scene is None and value.narration and value.text.startswith("A scripted")
