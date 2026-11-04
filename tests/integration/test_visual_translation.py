"""Keep resolved visual facts lossless before passing them to image generation."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.adapters.llm.images import ImageDescriber
from komaquill.api import Client
from langchain_core.messages import AIMessage


def test_english_facts_need_no_llm_and_keep_age_pose_and_empty_clothing():
    describer = ImageDescriber(SimpleNamespace(invoke=lambda *_a, **_kw: pytest.fail("LLM called")))
    result = describer.prepare_image(
        json.dumps(
            {
                "character": {
                    "age": "60",
                    "gender": "man",
                    "physical_features": "Gray hair",
                    "clothing": "",
                    "pose": "Sitting on a chair",
                    "held_objects": [],
                },
                "setting": {"place": "Beach", "scenery": "Wooden bench nearby"},
            }
        ),
        uuid4(),
        "lmstudio-4b",
        False,
        compose=True,
        purpose="conversation_image",
    )
    assert "age: 60." in result and "gender: man." in result
    assert "clothing: No clothing." in result and "held objects: Empty hands." in result
    assert "pose: Sitting on a chair." in result
    assert "Full body" in result and "scenery: Wooden bench nearby." in result


def test_absent_companion_uses_only_the_resolved_environment():
    describer = ImageDescriber(SimpleNamespace(invoke=lambda *_a, **_kw: pytest.fail("LLM called")))
    result = describer.prepare_image(
        json.dumps(
            {
                "character": None,
                "character_visible": False,
                "setting": {"place": "Beach", "time": "Evening"},
            }
        ),
        uuid4(),
        "lmstudio-4b",
        False,
        compose=True,
        purpose="conversation_image",
    )
    assert result == "place: Beach. time: Evening."


def test_portrait_keeps_explicit_empty_clothing_and_detects_its_change(
    tmp_path, substituted_services
):
    from komaquill.api import CharacterInput, CreationRequest
    from komaquill.library.rendering import preset_changes, preset_image_input

    api = Client.initialize(tmp_path)
    original = api.characters.create(CharacterInput(name="Portrait", initial_clothing=None))
    assert "initial_clothing" not in preset_image_input("character", original)
    value = CharacterInput(name="Portrait", initial_clothing="")
    assert preset_changes("character", value, original).image_changed
    saved = api.characters.update(original.id, original.revision, value)
    job, _ = api.library.accept(
        CreationRequest(
            operation_id=uuid4(),
            kind="portrait",
            target_id=saved.id,
            expected_revision=saved.revision,
        )
    )
    frozen = json.loads(job.original)
    assert frozen["initial_clothing"] == "" and frozen["initial_held_objects"] == []
    english = api.library.image_describer.prepare_image(
        job.original, job.id, "lmstudio-4b", False, compose=True, purpose="portrait"
    )
    assert "initial clothing: No clothing." in english
    assert "initial held objects: Empty hands." in english
    assert not preset_changes("character", value, saved).image_changed


@pytest.mark.parametrize("failure", [None, "missing", "japanese"])
def test_japanese_fields_translate_in_one_batch_with_bounded_repair(tmp_path, monkeypatch, failure):
    import langchain_openai

    calls = []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            schema = options["response_format"]["json_schema"]["schema"]
            assert set(schema["required"]) == {"gender", "physical_features", "pose"}
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            result = {"gender": "man", "physical_features": "Gray hair", "pose": "Clasped hands"}
            if len(calls) == 1:
                if failure == "missing":
                    result.pop("pose")
                elif failure == "japanese":
                    result["pose"] = "手を組む"
            return AIMessage(
                content=json.dumps(result, ensure_ascii=False),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    result = Client.initialize(tmp_path).library.image_describer.prepare_image(
        json.dumps(
            {
                "age": "60",
                "gender": "男",
                "appearance": "Gray hair",
                "physical_features": "白髪",
                "pose": "手を組む",
            },
            ensure_ascii=False,
        ),
        uuid4(),
        "lmstudio-4b",
        True,
        compose=True,
        purpose="portrait",
    )
    assert len(calls) == (1 if failure is None else 2)
    assert "age: 60." in result and "pose: Clasped hands." in result
    sent = json.loads(calls[0][-1].content)
    assert sent == {"gender": "男", "physical_features": "白髪", "pose": "手を組む"}
