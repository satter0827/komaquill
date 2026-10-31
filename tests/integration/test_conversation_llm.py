"""Exercise structured responses and repair without contacting a real model."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client
from komaquill.conversations.models import ConversationInput, SendInput
from komaquill.library.models import CreationRequest


@pytest.mark.parametrize("repair", [False, True])
def test_banner_extracts_environment_fields_instead_of_retelling_people_actions(
    tmp_path, monkeypatch, repair
):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls, schemas = [], []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            schemas.append(options["response_format"]["json_schema"]["schema"])
            return self

        def invoke(self, messages, config):
            calls.append(list(messages))
            return AIMessage(
                content=json.dumps(
                    {
                        "location": "古書店" if repair and len(calls) == 1 else "An old bookshop.",
                        "scenery": "Wooden shelves and a counter.",
                        "atmosphere": "Rain streaks the windows.",
                    },
                    ensure_ascii=False,
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    original = "A shopkeeper welcomes a traveler inside an old bookshop."
    result = Client.initialize(tmp_path).library.creator.prepare_image(
        original,
        uuid4(),
        "lmstudio-4b",
        True,
        compose=True,
        purpose="banner",
    )
    assert result == "An old bookshop. Wooden shelves and a counter. Rain streaks the windows."
    assert len(calls) == (2 if repair else 1)
    assert set(schemas[0]["properties"]) == {"location", "scenery", "atmosphere"}
    assert calls[0][-1].content == original


def test_reply_context_distinguishes_people_latest_scene_and_explicit_scene_changes(tmp_path):
    from types import SimpleNamespace

    from komaquill.conversations.models import Reply, StateCommand

    api = Client.initialize(tmp_path)
    original = api.conversations.responder
    conversation = api.conversations.create(
        ConversationInput(
            title="Scene",
            locale="en",
            user='{"name":"灯","age":"28","gender":"woman"}',
            character='{"name":"Mira","age":"32","gender":"woman","appearance":"green coat"}',
            scenario="A quiet bookshop",
        )
    )
    api.conversations.responder = SimpleNamespace(
        reply=lambda *_: Reply(
            text="Hello.",
            narration="Mira sits in the garden wearing a blue shirt.",
            state={"character_clothing": "green coat", "place": "garden", "quest": "find a book"},
            scene=None,
        )
    )
    api.conversations.send(
        conversation.id, SendInput(operation_id=uuid4(), text="Sit in the garden")
    )
    current = api.conversations.get(conversation.id)
    api.conversations.command(
        current.id,
        StateCommand(
            command="set_state",
            expected_revision=current.revision,
            state={**current.state, "place": "tower"},
        ),
    )
    calls = []
    original.invoke = lambda *_a, **_k: (
        calls.append(_a)
        or Reply(
            text="Here we are.",
            narration="Mira sits in the tower wearing a blue shirt.",
            state={},
            scene=None,
        )
    )
    original.reply(
        api.conversations.get(current.id),
        SendInput(
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    context = json.loads(calls[0][4].splitlines()[-2])
    assert context["definition"]["user"]["age"] == "28"
    assert context["definition"]["character"]["age"] == "32"
    assert context["definition"]["scenario"] == "A quiet bookshop"
    assert context["latest_scene"] == current.turns[-1].narration
    assert context["scene_settings"] == {"place": "tower"}
    assert context["state"] == {"place": "tower", "quest": "find a book"}
    assert api.conversations.get(current.id).turns == current.turns


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
                        "opening": "A visitor enters the shop.",
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
                        "narration": "Rain taps against the window.",
                        "text": "Here is the book.",
                        "state": {},
                        "scene": None,
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
            allow_real_llm=True,
        ),
    )
    context = json.loads(calls[0][0].content.splitlines()[-2])
    effective = context["definition"]
    assert effective["locale"] == "en" and effective["images"] is False
    assert effective["response_length"] == "detailed"
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
                content=json.dumps(
                    {
                        "name": "栞",
                        "description": "A quiet seaside bookshop.",
                        **(
                            {
                                "appearance": "Short silver hair",
                                "personality": "Calm",
                                "age": "unknown",
                                "gender": "unknown",
                            }
                            if kind == "character_draft"
                            else {"opening": "A visitor enters."}
                        ),
                    }
                ),
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
def test_image_description_requires_english_with_one_bounded_repair(
    tmp_path, monkeypatch, first_scene
):
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
                        "text": first_scene
                        if len(calls) == 1
                        else "An adult woman with short silver hair points at a book."
                    }
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    result = Client.initialize(tmp_path).library.creator.prepare_image(
        "短い銀髪の店主が本を指さす。", uuid4(), "lmstudio-4b", True
    )
    assert len(calls) == 2 and result.startswith("An adult woman")
    assert all(sum(message.type == "system" for message in messages) == 1 for messages in calls)


@pytest.mark.parametrize(
    "case", ["valid", "repair", "invalid", "wrong_model", "unavailable", "refusal"]
)
def test_structured_model_call_records_input_and_bounds_repair(tmp_path, monkeypatch, case):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []
    options = []
    response = json.dumps(
        {
            "narration": "Rain taps against the window.",
            "text": "A saved reply.",
            "state": {"place": "home"},
            "scene": None,
        }
    )

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
        ConversationInput(locale="en", title="Structured", llm_profile="lmstudio-4b")
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
                content=(
                    '{"text":"Saved","narration":"Rain taps against the window.",'
                    '"state":{},"scene":null}'
                ),
                response_metadata={"model_name": "qwen3.5-4b-uncensored-hauhaucs-aggressive"},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text(
        "[conversation]\nmax_input_characters=8000\n", encoding="utf-8"
    )
    conversation = api.conversations.create(
        ConversationInput(
            locale="en",
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


@pytest.mark.parametrize("invalid", ["empty", "template"])
def test_character_draft_repairs_missing_or_template_attributes(tmp_path, monkeypatch, invalid):
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
            age = "32" if len(calls) > 1 else ("" if invalid == "empty" else "{32}")
            return AIMessage(
                content=json.dumps(
                    {
                        "name": "Mira",
                        "age": age,
                        "gender": "woman",
                        "appearance": "Short red hair",
                        "personality": "Calm",
                    }
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    result = api.library.creator.draft(
        CreationRequest(
            operation_id=uuid4(),
            kind="character_draft",
            locale="en",
            prompt="Mira, a 32-year-old woman",
            allow_real_llm=True,
        )
    )
    assert result.age == "32" and result.gender == "woman"
    assert len(calls) == 2 and sum(message.type == "system" for message in calls[-1]) == 1


@pytest.mark.parametrize(
    "locale,language,wrong,right",
    [
        (
            "ja",
            "Japanese",
            "The navigator gestures at the greenhouse.",
            "案内役は温室を指し示した。",
        ),
        (
            "en",
            "English",
            "案内役は温室を指し示した。",
            "The navigator gestures at the greenhouse.",
        ),
    ],
)
def test_language_switch_schema_and_repair_target_narration(
    tmp_path, monkeypatch, locale, language, wrong, right
):
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
                        "text": "こんにちは。" if locale == "ja" else "Hello.",
                        "narration": wrong if len(calls) == 1 else right,
                        "state": {},
                        "scene": None,
                    },
                    ensure_ascii=False,
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(
            title="Switch", llm_profile="lmstudio-4b", locale="en" if locale == "ja" else "ja"
        )
    )
    from types import SimpleNamespace

    from komaquill.conversations.models import Reply

    responder = api.conversations.responder
    prior = "A previous English scene." if locale == "ja" else "前の場面の説明。"
    api.conversations.responder = SimpleNamespace(
        reply=lambda *_: Reply(text=prior, narration=prior, state={}, scene=None)
    )
    api.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Previous"))
    api.conversations.responder = responder
    operation = api.conversations.send(
        conversation.id,
        SendInput(operation_id=uuid4(), text="Continue", locale=locale, allow_real_llm=True),
    )
    assert operation.state == "complete" and len(calls) == 2
    assert language in schemas[0]["properties"]["narration"]["description"]
    assert "narration" in calls[-1][-1].content
    assert sum(message.type == "system" for message in calls[-1]) == 1
