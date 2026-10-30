"""Exercise structured responses and repair without contacting a real model."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client
from komaquill.conversations.models import ConversationInput, SendInput


def test_explicit_image_request_requires_an_english_scene_with_one_bounded_repair(
    tmp_path, monkeypatch
):
    import langchain_openai
    from langchain_core.messages import AIMessage

    schemas, calls = [], []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            schemas.append(options["response_format"]["json_schema"]["schema"])
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            return AIMessage(
                content=json.dumps(
                    {
                        "text": "栞は静かに窓辺の本を指さした。",
                        "state": {},
                        "scene": None
                        if len(calls) == 1
                        else "An adult woman with short silver hair and a black apron "
                        "points at a book beside a rainy window.",
                    }
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(title="Required image", llm_profile="lmstudio-4b", locale="ja")
    )
    reply = api.conversations.responder.reply(
        conversation,
        SendInput(operation_id=uuid4(), text="描いてください", images=True, allow_real_llm=True),
    )
    assert len(calls) == 2 and reply.scene.startswith("An adult woman")
    assert schemas[0]["properties"]["scene"]["type"] == "string"
    assert all(sum(message.type == "system" for message in messages) == 1 for messages in calls)


@pytest.mark.parametrize(
    "case", ["valid", "repair", "invalid", "wrong_model", "unavailable", "refusal"]
)
def test_structured_model_call_records_input_and_bounds_repair(tmp_path, monkeypatch, case):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []
    options = []
    response = json.dumps({"text": "A saved reply.", "state": {"place": "home"}, "scene": None})

    class Model:
        def __init__(self, **kwargs):
            options.append(kwargs)

        def bind(self, **kwargs):
            assert kwargs["response_format"]["type"] == "json_schema"
            return self

        def invoke(self, messages, config):
            calls.append(list(messages))
            assert config == {"callbacks": []}
            if case == "unavailable":
                raise RuntimeError("private wire error")
            invalid = case == "invalid" or (case == "repair" and len(calls) == 1)
            return AIMessage(
                content="invalid" if invalid else response,
                response_metadata={
                    "model_name": "wrong"
                    if case == "wrong_model"
                    else "qwen3.5-4b-uncensored-hauhaucs-aggressive"
                },
                additional_kwargs={"refusal": "refused"} if case == "refusal" else {},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    client = Client.initialize(tmp_path)
    conversation = client.conversations.create(
        ConversationInput(title="Structured", llm_profile="lmstudio-4b")
    )
    request = SendInput(operation_id=uuid4(), text="A private prompt", allow_real_llm=True)
    result = client.conversations.send(conversation.id, request)
    assert len(calls) == (2 if case in {"repair", "invalid"} else 1)
    assert options[0]["max_retries"] == 0 and options[0]["cache"] is False
    records = list(
        (tmp_path / ".artifacts/data/conversation-calls" / str(request.operation_id)).glob("*.json")
    )
    assert len(records) == len(calls)
    assert "A private prompt" in records[0].read_text()
    record = json.loads(records[0].read_bytes())
    assert len(record["instructions_sha256"]) == 64
    assert record["model"] == "qwen3.5-4b-uncensored-hauhaucs-aggressive" and "usage" in record
    assert record["status"] == ("failed" if case == "unavailable" else "response_received")
    if case != "unavailable":
        assert record["response_model"] == (
            "wrong" if case == "wrong_model" else "qwen3.5-4b-uncensored-hauhaucs-aggressive"
        )
    if case in {"valid", "repair"}:
        assert result.state == "complete"
        assert client.conversations.get(conversation.id).state == {"place": "home"}
    else:
        assert result.state == "failed" and client.conversations.get(conversation.id).turns == []
        assert "private" not in (result.error or "")


@pytest.mark.parametrize("case", ["fixed_context", "history"])
def test_context_limit_preserves_fixed_data_and_bounds_history(tmp_path, monkeypatch, case):
    import langchain_openai
    from komaquill.adapters.database import conversations
    from komaquill.conversations.models import Turn
    from langchain_core.messages import AIMessage

    calls = []

    class Model:
        def __init__(self, **kwargs):
            pass

        def bind(self, **kwargs):
            return self

        def invoke(self, messages, config):
            calls.append(list(messages))
            return AIMessage(
                content='{"text":"Saved","state":{},"scene":null}',
                response_metadata={"model_name": "qwen3.5-4b-uncensored-hauhaucs-aggressive"},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text(
        "[conversation]\nmax_input_characters=8000\n", encoding="utf-8"
    )
    conversation = api.conversations.create(
        ConversationInput(
            title="Budget",
            scenario="fixed scenario" + ("x" * 10000 if case == "fixed_context" else ""),
            llm_profile="lmstudio-4b",
        )
    )
    if case == "history":
        value = conversation.model_copy(
            update={
                "revision": 30,
                "turns": [
                    Turn(
                        operation_id=uuid4(),
                        user="user " + "x" * 1000,
                        assistant="reply " + "y" * 1000,
                        state={},
                    )
                    for _ in range(30)
                ],
            }
        )
        with api.conversations.repository.database.engine.begin() as connection:
            connection.execute(conversations.update().values(payload=value.model_dump_json()))
    result = api.conversations.send(
        conversation.id,
        SendInput(operation_id=uuid4(), text="Current message", allow_real_llm=True),
    )
    if case == "fixed_context":
        assert result.error == "context_limit_exceeded" and calls == []
    else:
        assert result.state == "complete" and len(calls) == 1
        assert len(calls[0]) < 27
        assert "fixed scenario" in calls[0][0].content
        assert sum(message.type == "system" for message in calls[0]) == 1
        assert calls[0][-1].content.startswith("Current message\n\n")
