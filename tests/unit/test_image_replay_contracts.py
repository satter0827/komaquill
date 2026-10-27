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


@pytest.mark.parametrize("name", ["klein-4b", "klein-4b-edit"])
def test_preset_fingerprint_survives_default_expansion_and_json_replay(name):
    settings = Settings(models=[name.removesuffix("-edit")])
    profile = GenerationPreset.model_validate(
        settings.presets[name].model_dump(mode="json", exclude_defaults=True)
    )
    settings = settings.model_copy(update={"presets": {name: profile}})
    compiled = compile_image_input(profile, "rainy street", subjects=("human", "cat"))
    retained = RuntimeSettings.model_validate_json(settings.model_dump_json())
    graph, ui = prepare(
        "ignored", 42, "test", settings=retained, compiled_input=compiled, profile_name=name
    )
    expected = prepare(
        "rainy street",
        42,
        "test",
        settings=settings,
        subject_prompts=("human", "cat"),
        profile_name=name,
    )
    assert (graph, ui) == expected


@pytest.mark.parametrize("subjects", [("",), ("  ", "cat"), ("human", "")])
def test_empty_visible_subject_is_rejected_before_any_request(
    project, session, client, stub, subjects
):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, "a street", 42, subjects=subjects)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0
    assert project.repository.load() == []


@pytest.mark.parametrize("seed", [-1, 2**64, -(2**64)])
def test_invalid_story_seed_is_not_silently_wrapped_or_persisted(project, session, seed):
    repository, plan = planned(project)
    active = session.model_copy(update={"settings": project.settings()})
    before = repository.renders(plan.request.story_id)
    with pytest.raises(KomaquillError) as error:
        create_render(repository, project, active, plan.request.story_id, "klein-4b", seed)
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
        generate(project, client, active, "a cat", seed)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0 and project.repository.load() == []


def old_compilation(profile):
    from komaquill.domain.prompts import joined

    value = compile_image_input(profile, "a cat").model_dump()
    value.update(
        format_version=3,
        compiler_version=6,
        preset_sha256="a" * 64,
        requested_negative=None,
        negative_prefix="",
        negative="",
        negative_body="",
        subject_positives=tuple(
            joined(profile.positive_prefix, text) for text in value["subject_prompts"]
        ),
        layout="global",
    )
    return value


def test_retained_old_compilation_is_readable_but_cannot_be_executed():
    from komaquill.domain.retained_images import RetainedImageInput

    settings = Settings()
    retained = RetainedImageInput.model_validate(old_compilation(settings.presets["klein-4b"]))
    original = retained.model_dump_json()
    assert RetainedImageInput.model_validate_json(original).model_dump_json() == original
    with pytest.raises(KomaquillError):
        prepare("unused", 42, "test", settings=settings, compiled_input=retained)


def test_self_consistent_but_wrong_quality_conditions_are_rejected():
    from komaquill.domain.images import CompiledImageInput

    settings = Settings(models=["klein-4b"])
    compiled = compile_image_input(settings.presets["klein-4b"], "street", subjects=("cat",))
    data = compiled.model_dump()
    data.update(quality_enabled=False, positive_prefix="", positive=data["body"])
    changed = CompiledImageInput.model_validate(data)
    with pytest.raises(KomaquillError) as error:
        prepare("unused", 42, "test", settings=settings, compiled_input=changed)
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


def test_maximum_valid_seed_wraps_only_subsequent_scene_seeds(project, session):
    repository, plan = planned(project)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(
        repository, project, active, plan.request.story_id, "klein-4b", 2**64 - 1
    )
    assert [scene.seed for scene in render.scenes] == [2**64 - 1, 0, 1, 2]


def test_legacy_blank_subject_metadata_remains_readable():
    from komaquill.domain.retained_images import RetainedImageInput

    profile = Settings(models=["klein-4b"]).presets["klein-4b"]
    value = old_compilation(profile)
    value.update(
        compiler_version=1, subject_prompts=(" ",), subject_positives=(profile.positive_prefix,)
    )
    retained = RetainedImageInput.model_validate(value)
    assert retained.compiler_version == 1 and retained.subject_prompts == (" ",)


@pytest.mark.parametrize("prompt,subjects", [(42, ()), ("a cat", (42,))])
def test_invalid_python_input_types_are_safe_and_do_not_submit(
    project, session, client, stub, prompt, subjects
):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, prompt, 42, subjects=subjects)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0 and project.repository.load() == []
