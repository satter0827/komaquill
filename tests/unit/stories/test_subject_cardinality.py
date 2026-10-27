"""Keep global Klein conditioning independent of the number of visible actors."""

import pytest

from komaquill.application.stories.image_inputs import scene_input
from komaquill.domain.images import ImageInput, compile_image_input
from komaquill.domain.rendering import SceneRenderConfig
from komaquill.domain.stories import SceneSubject, StoryDraft, validate_draft
from tests.support.stories import planned, request
from tests.unit.stories.test_visual_specification import example


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_actor_count_survives_planning_compilation_and_public_request(project, count):
    value = example()
    value["scenes"][0]["subjects"] = [
        {"character": actor["key"], "action": "stands beside the curb"}
        for actor in value["characters"][:count]
    ]
    value["scenes"][0]["props"] = []
    validate_draft(StoryDraft.model_validate(value), request())
    _, plan = planned(project)
    scene = plan.scenes[0].model_copy(
        update={
            "subjects": [
                SceneSubject.model_validate(row) for row in value["scenes"][0]["subjects"]
            ],
            "characters": [row["character"] for row in value["scenes"][0]["subjects"]],
            "props": [],
        }
    )
    profile = project.settings().presets["klein-4b"]
    compiled = scene_input(plan, scene, profile)
    assert len(compiled.subject_prompts) == count
    config = SceneRenderConfig(scene=1, profile="klein-4b", subjects=compiled.subject_prompts)
    image = ImageInput(prompt=compiled.prompt, subjects=config.subjects)
    assert compile_image_input(profile, image.prompt, subjects=image.subjects) == compiled


@pytest.mark.parametrize("holder", [None, "woman"])
def test_hidden_prop_state_is_retained_without_conditioning_an_extra_visible_object(
    project, holder
):
    from komaquill.domain.stories import ScenePropState, StoryProp

    _, plan = planned(project)
    coin = StoryProp(key="coin", name="硬貨", appearance="a copper coin")
    plan = plan.model_copy(update={"props": [*plan.props, coin]})
    state = ScenePropState(prop="coin", holder=holder, visible=False, state="inside a closed pouch")
    scene = plan.scenes[0].model_copy(update={"props": [*plan.scenes[0].props, state]})
    before = scene_input(plan, scene, project.settings().presets["klein-4b"])
    assert "copper coin" not in before.positive
    assert "closed pouch" not in before.positive
    assert scene.props[-1].visible is False
    visible = scene.model_copy(
        update={"props": [*scene.props[:-1], state.model_copy(update={"visible": True})]}
    )
    after = scene_input(plan, visible, project.settings().presets["klein-4b"])
    assert "copper coin" in after.positive
