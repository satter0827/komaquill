"""Check shared story inputs and the six standard model-operation workflows."""

import hashlib
from uuid import uuid4

import pytest

from komaquill.application.stories.image_inputs import scene_input
from komaquill.domain.images import compile_image_input
from komaquill.domain.materials import InputArtifact
from komaquill.domain.stories import PlannerProvenance, Scene, StoryPlan, validate_draft
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import prepare
from tests.support.stories import request
from tests.support.story_cases import cases


@pytest.mark.parametrize("case_id", list(cases()))
@pytest.mark.parametrize("model", ["klein-4b", "klein-9b", "wai-illustrious"])
@pytest.mark.parametrize("operation", ["generate", "edit"])
def test_same_visible_plan_is_portable_across_models_and_operations(case_id, model, operation):
    case = cases()[case_id]
    original = request(4, case.source)
    validate_draft(case.draft, original)
    instruction = "Paired offline fixture only"
    plan = StoryPlan(
        format_version=2,
        request=original,
        **case.draft.model_dump(exclude={"scenes"}),
        scenes=[
            Scene(
                **s.model_dump(),
                characters=[subject.character for subject in s.subjects],
                position=i,
                scene_id=uuid4(),
            )
            for i, s in enumerate(case.draft.scenes, 1)
        ],
        planner=PlannerProvenance(
            profile="fake",
            provider="fake",
            configured_model="fake",
            instructions=instruction,
            instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
            timeout_seconds=1,
            max_output_tokens=1,
            planning_policy={"style": "manga illustration"},
        ),
    )
    settings = Settings(models=[model])
    name = settings.profile_for(model, operation)
    profile = settings.presets[name]
    for scene in plan.scenes:
        compiled = scene_input(plan, scene, profile)
        if operation == "edit":
            reference = InputArtifact(
                kind="image",
                sha256="a" * 64,
                name="reference.png",
                size_bytes=100,
                width=768,
                height=1024,
                key="inputs/" + "a" * 64 + "/content",
            )
            compiled = compile_image_input(
                profile, compiled.prompt, compiled.subject_prompts, {"reference": reference}
            )
        graph, view = prepare(
            compiled.prompt,
            424242 + scene.position - 1,
            "offline",
            settings=settings,
            profile_name=name,
            compiled_input=compiled,
        )
        assert len(compiled.subject_prompts) == len(scene.subjects)
        assert "masterpiece" not in compiled.body
        assert graph and view["extra"]["linearData"]["inputs"]
        if model.startswith("klein-"):
            assert compiled.negative is None
            assert graph["63"]["class_type"] == "BasicGuider"
            assert graph["63"]["inputs"]["model"] == ["90", 0]
        else:
            assert (
                graph["90"]["inputs"]["strength_model"]
                == graph["90"]["inputs"]["strength_clip"]
                == ["92", 0]
            )
            assert graph["7"]["inputs"]["text"] == profile.negative_default


def test_model_selection_prepares_only_active_resource_dependencies():
    from komaquill.infrastructure.workflows import assets

    settings = Settings(models=["klein-4b", "wai-illustrious"])
    assert len(assets(settings)) == 6
    assert all(asset.model_family != "klein-9b" for asset in assets(settings))
    assert "klein-9b" in {p.model_id for p in settings.presets.values()}


@pytest.mark.parametrize("value", [0, 0.5, 1.0])
def test_wai_one_strength_input_controls_both_loader_branches(value):
    settings = Settings(models=["wai-illustrious"])
    profile = settings.presets["wai-illustrious"].configured_parameters({"detail_strength": value})
    settings = settings.model_copy(
        update={"presets": {**settings.presets, "wai-illustrious": profile}}
    )
    graph, _ = prepare("an empty room", 42, "test", settings=settings)
    assert graph["92"]["inputs"]["value"] == value
    assert (
        graph["90"]["inputs"]["strength_model"]
        == graph["90"]["inputs"]["strength_clip"]
        == ["92", 0]
    )


@pytest.mark.parametrize("model", ["unknown-model", "wai-illustrious"])
def test_material_model_rejection_is_typed_before_file_import(project, model):
    from komaquill.api import Client
    from komaquill.domain.errors import KomaquillError
    from komaquill.interfaces.image_inputs import material_inputs

    with pytest.raises(KomaquillError) as caught:
        material_inputs(
            Client(project.root), model, {"reference": "not-read.png"}, project.root, "edit"
        )
    assert caught.value.code == "invalid_configuration"
    assert not caught.value.retryable
    assert not (project.data / "inputs").exists()


def test_open_rejects_unknown_model_with_the_same_typed_failure(project, session, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from komaquill.domain.errors import KomaquillError
    from komaquill.interfaces import cli

    @contextmanager
    def operation(_):
        yield project

    @contextmanager
    def connected(_):
        yield (
            SimpleNamespace(json=lambda _: {}),
            session.model_copy(update={"settings": project.settings()}),
        )

    monkeypatch.setattr(cli, "operation", operation)
    monkeypatch.setattr(cli, "connected", connected)
    monkeypatch.setattr(cli, "verify_runtime", lambda *args: None)
    with pytest.raises(KomaquillError) as caught:
        cli.open_command(None, model="unknown-model")
    assert caught.value.code == "invalid_configuration" and not caught.value.retryable
