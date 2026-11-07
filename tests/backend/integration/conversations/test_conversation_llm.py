"""Exercise structured responses and repair without contacting a real model."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client
from komaquill.domain.conversations.models import ConversationInput, SendInput, TurnRequest
from komaquill.domain.library.models import CreationRequest
from komaquill.domain.roleplay import ConversationState

from tests.backend.support.conversations import set_state, wire_reply
from tests.backend.support.storage import call_files

pytestmark = pytest.mark.usefixtures("scripted_services")


def wire_state(value):
    state = ConversationState.model_validate(value)
    return {
        "relationship": state.relationship,
        "scenery": state.scenery,
        "character": state.character.model_dump(exclude={"appearance"}),
    }


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
            wire = options["response_format"]["json_schema"]["schema"]
            schemas.append(wire)
            if "response" in wire.get("properties", {}):
                assert set(wire["properties"]) == {"player_move", "response"}
            return self

        def invoke(self, messages, config):
            calls.append(list(messages))
            return AIMessage(
                content=json.dumps(
                    wire_reply(
                        {
                            "location": "古書店"
                            if repair and len(calls) == 1
                            else "An old bookshop.",
                            "scenery": "Wooden shelves and a counter.",
                            "atmosphere": "Rain streaks the windows.",
                        }
                    ),
                    ensure_ascii=False,
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    original = "A shopkeeper welcomes a traveler inside an old bookshop."
    result = Client.initialize(tmp_path).library.image_describer.prepare_image(
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

    from komaquill.adapters.llm.conversation import NarrativeReply
    from komaquill.domain.conversations.models import Reply

    api = Client.initialize(tmp_path)
    original = api.conversations.responder
    conversation = api.conversations.create(
        ConversationInput(
            title="Scene",
            locale="en",
            user={"name": "灯", "age": "28", "gender": "woman"},
            character={"name": "Mira", "age": "32", "gender": "woman", "appearance": "green coat"},
            scenario={"description": "A quiet bookshop", "goal": "find a book"},
            initial_state={"place": "garden"},
        )
    )
    api.conversations.responder = SimpleNamespace(
        reply=lambda *_: Reply(
            text="Hello.",
            narration="Mira sits in the garden wearing a blue shirt.",
            state_patch={
                "character": {"clothing": "blue shirt"},
            },
        )
    )
    api.conversations.send(
        conversation.id,
        SendInput(
            allow_real_llm=True,
            expected_revision=api.conversations.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Sit in the garden",
        ),
    )
    current = api.conversations.get(conversation.id)
    set_state(
        api, current.id, current.state.model_copy(update={"place": "tower"}), current.revision
    )
    calls = []
    original.execution.invoke = lambda *_a, **_k: (
        calls.append(_a)
        or NarrativeReply.model_validate(
            wire_reply(
                {
                    "action_narration": "",
                    "requested_player_action": None,
                    "current_state": wire_state(
                        {"place": "tower", "character": {"clothing": "blue shirt"}}
                    ),
                    "text": "Here we are.",
                }
            )
        )
    )
    original.reply(
        api.conversations.get(current.id),
        TurnRequest.model_validate_json(
            (
                SendInput(
                    expected_revision=0,
                    operation_id=uuid4(),
                    text="Continue",
                    allow_real_llm=True,
                )
            ).model_dump_json()
        ),
    )
    context = json.loads(
        next(line for line in reversed(calls[0][4].splitlines()) if line.startswith("{"))
    )
    assert context["people"]["human_player"]["name"] == "灯"
    assert context["people"]["human_player"]["age"] == "28"
    assert context["people"]["assistant_character"]["name"] == "Mira"
    assert context["people"]["assistant_character"]["age"] == "32"
    assert context["story"]["description"] == "A quiet bookshop"
    assert context["current_state"]["place"] == "tower"
    assert all("place" not in person for person in context["people"].values())
    assert "Decide player_move independently" in calls[0][4]
    assert context["people"]["assistant_character"]["clothing"] == "blue shirt"
    assert context["current_state"]["goal"] == "find a book"
    assert api.conversations.get(current.id).turns == current.turns


def test_completed_player_action_identifies_its_actor_and_keeps_companion_facts(tmp_path):
    from komaquill.adapters.llm.conversation import (
        DialogueResult,
        ObjectsChange,
        ReactionReply,
    )
    from komaquill.api import PrepareAction

    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(
            title="Separate people",
            locale="en",
            user={"name": "Akari", "age": "28", "initial_clothing": "Red coat"},
            character={"name": "Mira", "age": "60", "initial_clothing": "Black apron"},
            scenario={"place": "Bookshop"},
        )
    )
    calls = []
    corrections = []

    def invoke(*args, **kwargs):
        calls.append(args)
        corrections.append(kwargs["correction"])
        return ReactionReply.model_validate(
            {
                "response": {
                    "kind": "companion",
                    "changes": [],
                    "action_narration": "",
                    "text": "Understood.",
                },
            }
        )

    api._execution.invoke = invoke
    text = "I took off my coat and put it beside me."
    operation = api.conversations.send(
        scene.id,
        SendInput(
            operation_id=uuid4(),
            expected_revision=0,
            text=text,
            action=PrepareAction(clothing=""),
            allow_real_llm=True,
        ),
    )
    context = json.loads(next(line for line in calls[0][4].splitlines() if line.startswith("{")))
    assert calls[0][0] is ReactionReply
    assert context["response_rules"] == {
        "held_objects": ObjectsChange.model_fields["value"].description,
        "action_narration": DialogueResult.model_fields["action_narration"].description,
        "text": DialogueResult.model_fields["text"].description,
    }
    assert all(isinstance(rule, str) and rule for rule in context["response_rules"].values())
    assert context["people"]["human_player"]["clothing"] == ""
    assert context["people"]["assistant_character"]["clothing"] == "Black apron"
    assert context["people"]["assistant_character"]["name"] == "Mira"
    assert calls[0][4].endswith(corrections[0])
    assert "The completed_action is already applied" in corrections[0]
    assert "player_move" not in corrections[0]
    assert "Decide player_move independently" not in calls[0][4]
    latest = json.loads(calls[0][5][-1][1])
    assert latest == {
        "speaker": "human_player",
        "completed_action": {"actor": "human_player", "kind": "prepare", "clothing": ""},
        "message": text,
    }
    assert operation.state == "complete"
    saved = api.conversations.get(scene.id)
    assert saved.turns[0].user == text and saved.state.character.clothing == "Black apron"


def test_story_drafts_cannot_invent_a_saved_character_reference(tmp_path, monkeypatch):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []
    schemas = []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            wire = options["response_format"]["json_schema"]["schema"]
            schemas.append(wire)
            if "response" in wire.get("properties", {}):
                assert set(wire["properties"]) == {"player_move", "response"}
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            return AIMessage(
                content=json.dumps(
                    wire_reply(
                        {
                            "name": "A seaside shop",
                            "opening": "A visitor enters the shop.",
                            "character_id": str(uuid4()) if len(calls) == 1 else None,
                        }
                    )
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
                    wire_reply(
                        {
                            "action_narration": "",
                            "requested_player_action": None,
                            "current_state": wire_state({}),
                            "text": "Here is the book.",
                        }
                    )
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(
            title="Frozen",
            locale="ja",
            response_length="detailed",
            llm_profile="lmstudio-4b",
        )
    )
    api.conversations.responder.reply(
        conversation,
        TurnRequest.model_validate_json(
            (
                SendInput(
                    expected_revision=0,
                    operation_id=uuid4(),
                    text="Show the scene.",
                    locale="en",
                    allow_real_llm=True,
                )
            ).model_dump_json()
        ),
    )
    context = json.loads(
        next(line for line in reversed(calls[0][0].content.splitlines()) if line.startswith("{"))
    )
    assert context["response_language"] == "English"
    assert context["response_length"] == "detailed"
    assert "initial_state" not in context and "images" not in context
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
                    wire_reply(
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
                    )
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
                    wire_reply(
                        {
                            "status": "translated",
                            "text": first_scene
                            if len(calls) == 1
                            else "An adult woman with short silver hair points at a book.",
                        }
                    )
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    result = Client.initialize(tmp_path).library.image_describer.prepare_image(
        "短い銀髪の店主が本を指さす。", uuid4(), "lmstudio-4b", True
    )
    assert len(calls) == 2 and result.startswith("An adult woman")
    assert all(sum(message.type == "system" for message in messages) == 1 for messages in calls)


@pytest.mark.parametrize(
    "case",
    ["valid", "repair", "invalid", "wrong_model", "unavailable", "refusal", "length", "filter"],
)
def test_structured_model_call_records_input_and_bounds_repair(tmp_path, monkeypatch, case):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []
    options = []
    response = json.dumps(
        wire_reply(
            {
                "action_narration": "",
                "requested_player_action": None,
                "current_state": wire_state({}),
                "text": "A saved reply.",
            }
        )
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
            if case in {"length", "filter"}:
                from openai import ContentFilterFinishReasonError, LengthFinishReasonError
                from openai.types.chat import ChatCompletion

                completion = ChatCompletion(
                    id="bounded-output",
                    object="chat.completion",
                    created=0,
                    model="qwen3.5-4b-uncensored-hauhaucs-aggressive",
                    choices=[],
                )
                error = (
                    LengthFinishReasonError if case == "length" else ContentFilterFinishReasonError
                )
                raise error(completion=completion)
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
        ConversationInput(
            locale="en",
            title="Structured",
            llm_profile="lmstudio-4b",
        )
    )
    request = SendInput(
        expected_revision=0, operation_id=uuid4(), text="A private prompt", allow_real_llm=True
    )
    result = client.conversations.send(conversation.id, request)
    assert len(calls) == (2 if case in {"repair", "invalid"} else 1)
    assert options[0]["max_retries"] == 0 and options[0]["cache"] is False
    records = call_files(tmp_path, request.operation_id)
    assert len(records) == len(calls)
    assert "A private prompt" in records[0].read_text()
    record = json.loads(records[0].read_bytes())
    assert len(record["instructions_sha256"]) == 64
    assert record["workflow"] == {"engine": "langgraph", "version": 2}
    assert record["model"] == "qwen3.5-4b-uncensored-hauhaucs-aggressive" and "usage" in record
    assert record["status"] == (
        "failed" if case in {"unavailable", "length", "filter"} else "response_received"
    )
    if case in {"length", "filter"}:
        assert result.error == "invalid_response" and record["error"] == "invalid_response"
        assert record["usage"] is None and "response" not in record
    if case not in {"unavailable", "length", "filter"}:
        assert record["response_model"] == (
            "wrong" if case == "wrong_model" else "qwen3.5-4b-uncensored-hauhaucs-aggressive"
        )
    if case in {"valid", "repair"}:
        assert result.state == "complete"
        assert client.conversations.get(conversation.id).state.place == ""
    else:
        assert result.state == "failed" and client.conversations.get(conversation.id).turns == []
        assert "private" not in (result.error or "")


@pytest.mark.parametrize("case", ["fixed_context", "history"])
def test_context_limit_preserves_fixed_data_and_bounds_history(tmp_path, monkeypatch, case):
    import langchain_openai
    from komaquill.domain.conversations.models import Turn
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
                    '"state_patch":{},"scene":null}'
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
            scenario={"world": "fixed scenario" + ("x" * 5900 if case == "fixed_context" else "")},
            llm_profile="lmstudio-4b",
        )
    )
    if case == "history":
        value = conversation.model_copy(
            update={
                "revision": 30,
                "turn_count": 30,
                "turns": [
                    Turn(
                        operation_id=uuid4(),
                        user=f"user {index} " + "x" * 1000,
                        assistant="reply " + "y" * 1000,
                        state={},
                    )
                    for index in range(30)
                ],
            }
        )
        with api.conversations.repository.database.engine.begin() as connection:
            api.conversations.repository._store(connection, value)
    result = api.conversations.send(
        conversation.id,
        SendInput(
            expected_revision=api.conversations.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Current message",
            allow_real_llm=True,
        ),
    )
    assert result.error == "context_limit_exceeded" and calls == []


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
                    wire_reply(
                        {
                            "name": "Mira",
                            "age": age,
                            "gender": "woman",
                            "appearance": "Short red hair",
                            "personality": "Calm",
                        }
                    )
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
            wire = options["response_format"]["json_schema"]["schema"]
            schemas.append(wire)
            if "response" in wire.get("properties", {}):
                assert set(wire["properties"]) == {"player_move", "response"}
                for field in ("text", "action_narration"):
                    assert (
                        language
                        in wire["$defs"]["DialogueResult"]["properties"][field]["description"]
                    )
                assert (
                    language in wire["$defs"]["SceneryChange"]["properties"]["value"]["description"]
                )
                assert (
                    language
                    not in wire["$defs"]["ClothingChange"]["properties"]["value"]["description"]
                )
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            return AIMessage(
                content=json.dumps(
                    wire_reply(
                        {
                            "action_narration": "",
                            "requested_player_action": None,
                            "current_state": wire_state(
                                {"scenery": wrong if len(calls) == 1 else right}
                            ),
                            "text": "こんにちは。" if locale == "ja" else "Hello.",
                        }
                    ),
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

    from komaquill.domain.conversations.models import Reply

    responder = api.conversations.responder
    prior = "A previous English scene." if locale == "ja" else "前の場面の説明。"
    api.conversations.responder = SimpleNamespace(
        reply=lambda *_: Reply(text=prior, narration=prior, state_patch={})
    )
    api.conversations.send(
        conversation.id,
        SendInput(
            allow_real_llm=True,
            expected_revision=api.conversations.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Previous",
        ),
    )
    api.conversations.responder = responder
    operation = api.conversations.send(
        conversation.id,
        SendInput(
            expected_revision=api.conversations.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Continue",
            locale=locale,
            allow_real_llm=True,
        ),
    )
    assert operation.state == "complete" and len(calls) == 2
    assert language in calls[0][0].content
    assert "scenery" in calls[-1][-1].content
    assert sum(message.type == "system" for message in calls[-1]) == 1


def test_owned_object_repair_identifies_the_path_without_copying_its_value(tmp_path, monkeypatch):
    import langchain_openai
    from langchain_core.messages import AIMessage

    calls = []
    object_id = "retained-private-object"

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **_):
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            return AIMessage(
                content=json.dumps(
                    {
                        "player_move": None,
                        "response": {
                            "kind": "companion",
                            "changes": [
                                {
                                    "field": "held_objects",
                                    "value": [{"id": object_id, "name": "Parcel"}],
                                }
                            ]
                            if len(calls) == 1
                            else [],
                            "action_narration": "",
                            "text": "Hello, Akari.",
                        },
                    }
                ),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(
            title="Ownership",
            locale="en",
            user={"name": "Akari", "initial_held_objects": [{"id": object_id, "name": "Parcel"}]},
            character={"name": "Mira"},
        )
    )
    result = api.conversations.send(
        scene.id,
        SendInput(
            operation_id=uuid4(),
            expected_revision=0,
            text="I keep my parcel. Hello.",
            allow_real_llm=True,
        ),
    )
    assert result.state == "complete" and len(calls) == 2
    correction = calls[-1][-1].content
    assert '"path": ["response", "changes", 0, "value", 0, "id"]' in correction
    assert "object_ownership" in correction and object_id not in correction
    saved = api.conversations.get(scene.id)
    assert saved.state.player.held_objects[0].id == object_id
    assert not saved.state.character.held_objects
