"""Exercise structured responses and repair without contacting a real model."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client
from komaquill.conversations.models import ConversationInput, SendInput
from komaquill.library.models import CreationRequest


def test_story_drafts_cannot_invent_a_saved_character_reference(tmp_path, monkeypatch):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []
    schemas = []

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
                        "name": "A seaside shop",
                        "character_id": str(uuid4()) if len(calls) == 1 else None,
                    }
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    result = api.library.creator.draft(
        CreationRequest(
            operation_id=uuid4(),
            kind="story_draft",
            prompt="A seaside shop",
            locale="en",
            llm_profile="lmstudio-4b",
            allow_real_llm=True,
        )
    )
    assert len(calls) == 2 and result.character_id is None
    assert schemas[0]["properties"]["character_id"]["type"] == "null"
    assert api.stories.get(api.stories.create(result).id).name == "A seaside shop"


def test_operation_conditions_replace_frozen_prompt_defaults(tmp_path, monkeypatch):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            return AIMessage(
                content=json.dumps(
                    {
                        "text": "Here is the book.",
                        "state": {},
                        "scene": "An adult woman points at a book.",
                    }
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(
            title="Frozen",
            locale="ja",
            images=False,
            response_length="detailed",
            llm_profile="lmstudio-4b",
        )
    )
    api.conversations.responder.reply(
        conversation,
        SendInput(
            operation_id=uuid4(),
            text="Show the scene.",
            locale="en",
            images=True,
            response_length="short",
            allow_real_llm=True,
        ),
    )
    context = json.loads(calls[0][0].content.splitlines()[-2])
    effective = context["definition"]
    assert effective["locale"] == "en" and effective["images"] is True
    assert effective["response_length"] == "short"
    assert api.conversations.get(conversation.id).definition == conversation.definition


@pytest.mark.parametrize("kind", ["character_draft", "story_draft"])
def test_english_drafts_preserve_requested_proper_names(tmp_path, monkeypatch, kind):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            return AIMessage(
                content=json.dumps({"name": "栞", "description": "A quiet seaside bookshop."}),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    result = api.library.creator.draft(
        CreationRequest(
            operation_id=uuid4(),
            kind=kind,
            prompt="Keep the name 栞.",
            locale="en",
            llm_profile="lmstudio-4b",
            allow_real_llm=True,
        )
    )
    assert result.name == "栞" and len(calls) == 1


@pytest.mark.parametrize("first_scene", [None, "栞 points at a book."])
def test_explicit_image_request_requires_an_english_scene_with_one_bounded_repair(
    tmp_path, monkeypatch, first_scene
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
                        "scene": first_scene
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
        ConversationInput(
            title="Required image",
            llm_profile="lmstudio-4b",
            locale="ja",
            character=json.dumps({"name": "栞"}),
        )
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
