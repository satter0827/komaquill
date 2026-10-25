"""Validate visible actors and meaningful props without grading old plans."""

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from komaquill.domain.stories import StoryDraft, StoryPlan, validate_draft
from tests.support.stories import planned, request


def example():
    return json.loads(
        files("komaquill")
        .joinpath("resources/stories/examples/rainy-cat/plan-4.json")
        .read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "fault",
    [
        "category",
        "missing_subjects",
        "three",
        "c99",
        "duplicate",
        "prop",
        "holder",
        "duplicate_prop",
        "language",
    ],
)
def test_reject_invalid_visual_specifications(fault):
    value = example()
    scene = value["scenes"][0]
    if fault == "category":
        value["characters"][0].pop("kind")
    elif fault == "missing_subjects":
        scene.pop("subjects")
    elif fault == "three":
        scene["subjects"].append({"character": "c3", "action": "sits"})
    elif fault == "c99":
        scene["subjects"][0]["character"] = "c99"
    elif fault == "duplicate":
        scene["subjects"][1] = scene["subjects"][0]
    elif fault == "prop":
        scene["props"][0]["prop"] = "o99"
    elif fault == "holder":
        scene["props"][0]["holder"] = "c3"
    elif fault == "duplicate_prop":
        value["props"].append(value["props"][0])
    else:
        scene["subjects"][0]["action"] = "\u5098\u3092\u6301\u3064"
    with pytest.raises((ValueError, ValidationError)):
        validate_draft(StoryDraft.model_validate(value), request())


def test_retained_plan_keeps_unknown_categories_and_more_than_two_actors(project):
    _, plan = planned(project)
    old = plan.model_dump(mode="json")
    old["format_version"] = 1
    old.pop("props")
    for character in old["characters"]:
        character.pop("kind")
    for scene in old["scenes"]:
        scene.pop("subjects")
        scene.pop("props")
    old["scenes"][-1]["characters"] = ["c1", "c2", "c3"]
    retained = StoryPlan.model_validate(old)
    assert retained.model_dump(mode="json", exclude_unset=True) == old
    assert retained.characters[0].kind is None
    assert retained.scenes[-1].subjects is None


def test_new_plan_rejects_inconsistent_retained_actor_index(project):
    _, plan = planned(project)
    value = plan.model_dump(mode="json")
    value["scenes"][0]["characters"] = ["c1"]
    with pytest.raises(ValueError):
        StoryPlan.model_validate(value)


def test_repair_reports_language_and_reference_defects_together():
    from komaquill.domain.stories import PlanningValidationError

    value = example()
    value["scenes"][0]["subjects"][0]["character"] = "c99"
    value["characters"][0]["appearance"] = "\u4eba\u7269"
    with pytest.raises(PlanningValidationError) as error:
        validate_draft(StoryDraft.model_validate(value), request())
    codes = {issue.code for issue in error.value.issues}
    assert {"invalid_structure", "invalid_visual_reference", "visual_language"} <= codes


@pytest.mark.parametrize("holder", ["null", "mother", "母猫"])
def test_new_planning_schema_rejects_ambiguous_holder_ids(holder):
    value = example()
    value["scenes"][0]["props"][0]["holder"] = holder
    with pytest.raises(ValidationError):
        StoryDraft.model_validate(value)


def test_same_framing_with_different_action_is_a_distinct_moment():
    value = example()
    value["scenes"][1]["visual_description"] = value["scenes"][0]["visual_description"]
    validate_draft(StoryDraft.model_validate(value), request())


def test_reference_issues_identify_every_invalid_prop_field():
    from komaquill.domain.stories import PlanningValidationError

    value = example()
    value["scenes"][0]["props"][0].update(prop="o99", holder="c3")
    with pytest.raises(PlanningValidationError) as error:
        validate_draft(StoryDraft.model_validate(value), request())
    paths = {issue.path for issue in error.value.issues}
    assert ("scenes", 0, "props", 0, "prop") in paths
    assert ("scenes", 0, "props", 0, "holder") in paths
