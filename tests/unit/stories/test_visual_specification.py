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
        "unknown",
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
        scene["subjects"].append({"character": "mother", "action": "sits"})
    elif fault == "unknown":
        scene["subjects"][0]["character"] = "unknown"
    elif fault == "duplicate":
        scene["subjects"][1] = scene["subjects"][0]
    elif fault == "prop":
        scene["props"][0]["prop"] = "unknown"
    elif fault == "holder":
        scene["props"][0]["holder"] = "mother"
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
    old["scenes"][-1]["characters"] = ["mio", "kitten", "mother"]
    retained = StoryPlan.model_validate(old)
    assert retained.model_dump(mode="json", exclude_unset=True) == old
    assert retained.characters[0].kind is None
    assert retained.scenes[-1].subjects is None


def test_new_plan_rejects_inconsistent_retained_actor_index(project):
    _, plan = planned(project)
    value = plan.model_dump(mode="json")
    value["scenes"][0]["characters"] = ["mio"]
    with pytest.raises(ValueError):
        StoryPlan.model_validate(value)
