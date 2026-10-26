"""Check effective prompts, standard regions and immutable replay contracts."""

import pytest
from pydantic import ValidationError

from komaquill.application.generation import generate
from komaquill.application.stories.rendering import create_render, render_story
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import CompiledImageInput, compile_image_input
from komaquill.domain.presets import ConditioningRegion, GenerationPreset
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import assets, prepare
from tests.support.stories import planned


def preset(name="pony"):
    return Settings(profiles=[name]).presets[name]


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("negative", [None, "", "extra limbs"])
def test_quality_prefixes_are_optional_and_do_not_replace_user_negative(enabled, negative):
    profile = preset().model_copy(
        update={"quality_enabled": enabled, "negative": "default avoidance"}
    )
    value = compile_image_input(profile, "a human beside a cat", negative)
    assert value.prompt == "a human beside a cat"
    assert value.requested_negative == negative
    assert value.positive == (profile.positive_prefix + ", " if enabled else "") + value.prompt
    body = "default avoidance" if negative is None else negative
    assert value.negative == ", ".join(
        part for part in (profile.negative_prefix if enabled else "", body) if part
    )
    assert CompiledImageInput.model_validate_json(value.model_dump_json()) == value


def test_compilation_uses_global_or_independent_subject_conditioning():
    subjects = ("human woman in a yellow raincoat", "small gray quadruped kitten")
    global_input = compile_image_input(preset(), "rainy curb", subjects=subjects)
    regional = compile_image_input(preset("pony-story"), "rainy curb", subjects=subjects)
    assert global_input.body == "rainy curb, " + ", ".join(subjects)
    assert regional.body == "rainy curb" and regional.layout == "left-right"
    assert regional.subject_prompts == subjects
    assert all(text.count("score_9") == 1 for text in regional.subject_positives)
    value = regional.model_dump()
    value["positive"] = "changed"
    with pytest.raises(ValidationError):
        CompiledImageInput.model_validate(value)
    with pytest.raises(ValidationError):
        compile_image_input(preset(), "crowd", subjects=("one", "two", "three"))


@pytest.mark.parametrize("subjects", [(), ("human woman",), ("human woman", "gray kitten")])
def test_regional_graph_matches_native_widgets_and_disables_unused_slots(subjects):
    settings = Settings(profiles=["pony-story"])
    value = compile_image_input(settings.presets["pony-story"], "rainy street", subjects=subjects)
    graph, ui = prepare("ignored", None, 42, "request", settings=settings, compiled_input=value)
    profile = settings.presets["pony-story"]
    for binding in profile.bindings:
        widgets = next(node for node in ui["nodes"] if node["id"] == binding.ui_node)[
            "widgets_values"
        ]
        assert graph[binding.api_node]["inputs"][binding.api_input] == widgets[binding.ui_widget]

    def read(source):
        row = next(row for row in profile.bindings if row.source == source)
        return graph[row.api_node]["inputs"][row.api_input]

    assert read("subject_1") == (value.subject_positives[0] if subjects else "")
    assert read("region_1_strength") == (1.0 if subjects else 0.0)
    assert read("region_2_strength") == (1.0 if len(subjects) == 2 else 0.0)
    assert read("region_1_width") == (0.5 if len(subjects) == 2 else 1.0)
    wrong = compile_image_input(preset(), "rainy street")
    with pytest.raises(KomaquillError):
        prepare("ignored", None, 42, "request", settings=settings, compiled_input=wrong)


@pytest.mark.parametrize("change", [{"x": 0.8}, {"width": 0}, {"height": float("nan")}])
def test_regions_reject_invalid_geometry(change):
    with pytest.raises(ValidationError):
        ConditioningRegion.model_validate({"width": 0.5, **change})


def test_regional_bundle_rejects_missing_slots_and_reuses_checkpoint():
    profile = preset("pony-story").model_dump()
    profile["bindings"] = [row for row in profile["bindings"] if row["source"] != "subject_2"]
    with pytest.raises(ValidationError):
        GenerationPreset.model_validate(profile)
    assert len(assets(Settings(profiles=["pony", "pony-story"]))) == 1


def test_quality_disabled_is_identical_in_api_and_browser():
    settings = Settings(profiles=["pony"])
    profile = settings.presets["pony"].model_copy(update={"quality_enabled": False})
    settings = settings.model_copy(update={"presets": {**settings.presets, "pony": profile}})
    graph, ui = prepare("a cat", "custom", 42, "request", settings=settings)
    for source, expected in [
        ("positive_prefix", ""),
        ("prompt_delimiter", ""),
        ("negative", "custom"),
    ]:
        row = next(row for row in profile.bindings if row.source == source)
        assert graph[row.api_node]["inputs"][row.api_input] == expected
        assert (
            next(n for n in ui["nodes"] if n["id"] == row.ui_node)["widgets_values"][row.ui_widget]
            == expected
        )


def test_story_resume_never_recompiles_or_applies_quality_twice(
    project, session, client, monkeypatch
):
    from komaquill.application.stories import rendering
    from komaquill.infrastructure import workflows

    settings = Settings(profiles=["pony", "pony-story"])
    active = session.model_copy(update={"settings": settings})
    repository, plan = planned(project)
    render = create_render(repository, project, active, plan.request.story_id, "pony-story", 42)
    assert render.format_version == 5

    def unexpected(*args, **kwargs):
        pytest.fail("Resume recompiled a previously frozen scene")

    monkeypatch.setattr(rendering, "scene_input", unexpected)
    monkeypatch.setattr(workflows, "compile_image_input", unexpected)
    result = render_story(repository, project, client, active, render.render_id)
    assert result["complete"] and result["props"]
    for scene, expected in zip(result["scenes"], render.scenes, strict=True):
        from uuid import UUID

        run = project.repository.read(UUID(scene["run_id"]))
        assert run.compiled_input == expected.compiled_input
        assert scene["compiled_input"] == expected.compiled_input.model_dump(mode="json")
        assert run.compiled_input.positive.count("score_9") == 1
    assert render_story(repository, project, client, active, render.render_id)["complete"]


def test_single_image_history_retains_original_and_effective_prompts(project, session, client):
    active = session.model_copy(update={"settings": project.settings()})
    run = generate(project, client, active, "a human beside a cat", "extra limbs", 42)
    assert run.compiled_input.prompt == "a human beside a cat"
    assert run.compiled_input.negative.endswith(", extra limbs")
    assert run.compiled_input.quality_enabled


def test_old_unstarted_render_is_not_modified_when_replanning_is_required(project, session, client):
    from komaquill.domain.stories import StoryRender

    repository, plan = planned(project)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "pony", 42)
    data = render.model_dump(mode="json")
    data["format_version"] = 1
    for scene in data["scenes"]:
        scene.pop("compiled_input")
    old = StoryRender.model_validate(data)
    repository.save_render(old)
    path = (
        repository.folder(plan.request.story_id) / "renders" / str(render.render_id) / "render.json"
    )
    before = path.read_bytes()
    with pytest.raises(KomaquillError) as error:
        render_story(repository, project, client, active, render.render_id)
    assert error.value.code == "story_replan_required"
    assert path.read_bytes() == before


def test_invalid_subject_input_fails_before_submission(project, session, client, stub):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, "crowd", None, 42, subjects=("a", "b", "c"))
    assert error.value.code == "invalid_request" and stub.submissions == 0
