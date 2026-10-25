"""Verify immutable image inputs across public configuration and replay boundaries."""

import json

import pytest

from komaquill.application.generation import generate
from komaquill.application.stories.rendering import create_render
from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import compile_image_input
from komaquill.domain.presets import GenerationPreset
from komaquill.domain.stories import PlanningValidationError, StoryDraft, validate_draft
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import prepare
from tests.support.stories import planned, request


@pytest.mark.parametrize("name", ["pony", "pony-story"])
def test_preset_fingerprint_survives_default_expansion_and_json_replay(name):
    settings = Settings(profiles=[name])
    profile = GenerationPreset.model_validate(
        settings.presets[name].model_dump(mode="json", exclude_defaults=True)
    )
    settings = settings.model_copy(update={"presets": {name: profile}})
    compiled = compile_image_input(profile, "rainy street", subjects=("human", "cat"))
    retained = RuntimeSettings.model_validate_json(settings.model_dump_json())
    graph, ui = prepare("ignored", None, 42, "test", settings=retained, compiled_input=compiled)
    expected = prepare(
        "rainy street", None, 42, "test", settings=settings, subject_prompts=("human", "cat")
    )
    assert (graph, ui) == expected


@pytest.mark.parametrize("subjects", [("",), ("  ", "cat"), ("human", "")])
def test_empty_visible_subject_is_rejected_before_any_request(
    project, session, client, stub, subjects
):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, "a street", None, 42, subjects=subjects)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0
    assert project.repository.load() == []


@pytest.mark.parametrize("seed", [-1, 2**64, -(2**64)])
def test_invalid_story_seed_is_not_silently_wrapped_or_persisted(project, session, seed):
    repository, plan = planned(project)
    active = session.model_copy(update={"settings": project.settings()})
    before = repository.renders(plan.request.story_id)
    with pytest.raises(KomaquillError) as error:
        create_render(repository, project, active, plan.request.story_id, "pony", seed)
    assert error.value.code == "invalid_request"
    assert repository.renders(plan.request.story_id) == before


@pytest.mark.parametrize("identifier", ["c2", "o1", "p1"])
def test_visible_reference_ids_are_not_diffusion_prompt_text(identifier):
    from importlib.resources import files

    value = json.loads(
        files("komaquill")
        .joinpath("resources/stories/examples/rainy-cat/plan-4.json")
        .read_text(encoding="utf-8")
    )
    value["scenes"][0]["props"][0]["state"] = "held above " + identifier
    with pytest.raises(PlanningValidationError) as error:
        validate_draft(StoryDraft.model_validate(value), request())
    assert any(issue.path == ("scenes", 0, "props", 0, "state") for issue in error.value.issues)


@pytest.mark.parametrize("seed", [True, 0.5, -1, 2**64])
def test_single_image_seed_is_rejected_before_backend_or_history(
    project, session, client, stub, seed
):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, "a cat", None, seed)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0 and project.repository.load() == []


def test_legacy_compiler_replay_uses_the_retained_hash_without_recompiling():
    from komaquill.domain.images import CompiledImageInput, preset_fingerprint

    settings = Settings(profiles=["pony"])
    profile = settings.presets["pony"]
    old = compile_image_input(profile, "a cat").model_copy(
        update={
            "compiler_version": 1,
            "preset_sha256": preset_fingerprint(profile, 1),
        }
    )
    old = CompiledImageInput.model_validate_json(old.model_dump_json())
    graph, _ = prepare("unused", None, 42, "test", settings=settings, compiled_input=old)
    expected, _ = prepare("a cat", None, 42, "test", settings=settings)
    assert graph == expected


@pytest.mark.parametrize("change", ["quality", "layout"])
def test_self_consistent_but_wrong_frozen_conditions_are_rejected(change):
    from komaquill.domain.images import CompiledImageInput, joined

    settings = Settings(profiles=["pony-story"])
    compiled = compile_image_input(settings.presets["pony-story"], "street", subjects=("cat",))
    data = compiled.model_dump()
    if change == "quality":
        data.update(
            quality_enabled=False,
            positive_prefix="",
            negative_prefix="",
            positive="street",
            negative="",
            subject_positives=("cat",),
        )
    else:
        data.update(
            layout="global",
            body=joined(compiled.prompt, *compiled.subject_prompts),
            positive=joined(compiled.positive_prefix, compiled.prompt, *compiled.subject_prompts),
        )
    changed = CompiledImageInput.model_validate(data)
    with pytest.raises(KomaquillError) as error:
        prepare("unused", None, 42, "test", settings=settings, compiled_input=changed)
    assert error.value.code == "invalid_configuration"


def test_invalid_public_story_seed_does_not_call_llm_or_backend(project, monkeypatch):
    from komaquill.api import Client, stories
    from tests.support.stories import source

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid inputs must fail before runtime or LLM access")

    monkeypatch.setattr(stories, "connected", unexpected)
    with pytest.raises(KomaquillError) as error:
        list(Client(project.root).stories.generate(source(), llm_profile="fake", seed=-1))
    assert error.value.code == "invalid_request"


def test_scene_limit_cannot_exceed_supported_planning_contract(project):
    from pydantic import ValidationError

    from komaquill.infrastructure.stories.configuration import settings

    with pytest.raises(ValidationError):
        settings(project, max_images=9)


def split_negative_settings(enabled):
    settings = Settings()
    profile = settings.presets["pony"].model_copy(
        update={"quality_enabled": enabled, "negative": "extra limbs"}
    )
    negative = next(row for row in profile.bindings if row.source == "negative")
    return settings.model_copy(update={"presets": {"pony": profile}}), negative.api_node


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("negative", [None, "", "blur"])
def test_split_negative_quality_is_applied_once_and_disabled_consistently(enabled, negative):
    settings, identity = split_negative_settings(enabled)
    profile = settings.presets["pony"]
    graph, ui = prepare("a cat", negative, 42, "test", settings=settings)
    values = graph[identity]["inputs"]
    body = profile.negative if negative is None else negative
    assert values["string_a"] == (profile.negative_prefix if enabled else "")
    assert values["string_b"] == body
    assert values["delimiter"] == ", "
    expected = compile_image_input(profile, "a cat", negative)
    import re

    cleaner = next(node for node in graph.values() if node["class_type"] == "RegexReplace")
    assert cleaner["inputs"]["string"] == [identity, 0]
    actual = re.sub(
        cleaner["inputs"]["regex_pattern"],
        cleaner["inputs"]["replace"],
        values["delimiter"].join((values["string_a"], values["string_b"])),
    )
    assert actual == expected.negative
    assert actual.count("text, watermark, signature") == (1 if enabled else 0)
    for row in profile.bindings:
        node = next(n for n in ui["nodes"] if n["id"] == row.ui_node)
        assert graph[row.api_node]["inputs"][row.api_input] == node["widgets_values"][row.ui_widget]


def test_maximum_valid_seed_wraps_only_subsequent_scene_seeds(project, session):
    repository, plan = planned(project)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "pony", 2**64 - 1)
    assert [scene.seed for scene in render.scenes] == [2**64 - 1, 0, 1, 2]


def test_legacy_blank_subject_metadata_is_read_without_applying_new_quality_rules():
    from komaquill.domain.images import CompiledImageInput, preset_fingerprint

    profile = Settings(profiles=["pony"]).presets["pony"]
    value = compile_image_input(profile, "a street").model_dump()
    value.update(
        compiler_version=1,
        preset_sha256=preset_fingerprint(profile, 1),
        subject_prompts=(" ",),
        subject_positives=(profile.positive_prefix,),
    )
    retained = CompiledImageInput.model_validate(value)
    assert retained.compiler_version == 1 and retained.subject_prompts == (" ",)
    assert retained.matches(profile)


@pytest.mark.parametrize(
    "prompt,negative,subjects", [(42, None, ()), ("a cat", True, ()), ("a cat", None, (42,))]
)
def test_invalid_python_input_types_are_safe_and_do_not_submit(
    project, session, client, stub, prompt, negative, subjects
):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, prompt, negative, 42, subjects=subjects)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0 and project.repository.load() == []
