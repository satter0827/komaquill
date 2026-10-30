"""Reject changed identities and broken references in retained work without rewriting it."""

from copy import deepcopy
from uuid import uuid4

import pytest
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.locations import Locations
from komaquill.adapters.workspace import find_project
from komaquill.api import Client
from komaquill.archive.models import StoryInput, StoryPlan, StoryRender
from komaquill.domain.workflow import digest

from tests.integration.test_archive import retained


def visual_plan(tmp_path):
    client = Client.initialize(tmp_path)
    identity, folder = retained(client)
    value = client.archive.get(identity)["plan"]
    value.update(
        format_version=2,
        characters=[{"key": "person", "kind": "human", "name": "Alex", "appearance": "green coat"}],
        props=[{"key": "cup", "name": "Cup", "appearance": "blue cup"}],
    )
    value["scenes"][0].update(
        characters=["person"],
        subjects=[{"character": "person", "action": "standing"}],
        props=[{"prop": "cup", "holder": "person", "visible": True, "state": "held"}],
    )
    return value


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "no_kind",
        "duplicate_actor",
        "bad_source",
        "unknown_actor",
        "position",
        "duplicate_scene",
        "duplicate_prop",
        "no_subjects",
        "duplicate_subject",
        "unknown_prop",
        "duplicate_scene_prop",
        "invisible_holder",
        "unknown_holder",
    ],
)
def test_archive_plan_checks_durable_references(tmp_path, case):
    value = visual_plan(tmp_path)
    scene = value["scenes"][0]
    if case == "no_kind":
        value["characters"][0]["kind"] = None
    elif case == "duplicate_actor":
        value["characters"].append(deepcopy(value["characters"][0]))
    elif case == "bad_source":
        scene["source_refs"] = ["missing"]
    elif case == "unknown_actor":
        scene["characters"] = ["missing"]
    elif case == "position":
        scene["position"] = 2
    elif case == "duplicate_scene":
        value["request"]["image_count"] = 2
        value["scenes"].append(deepcopy(scene))
        value["scenes"][1]["position"] = 2
    elif case == "duplicate_prop":
        value["props"].append(deepcopy(value["props"][0]))
    elif case == "no_subjects":
        scene["subjects"] = None
    elif case == "duplicate_subject":
        scene["subjects"].append(deepcopy(scene["subjects"][0]))
    elif case == "unknown_prop":
        scene["props"][0]["prop"] = "missing"
    elif case == "duplicate_scene_prop":
        scene["props"].append(deepcopy(scene["props"][0]))
    elif case == "invisible_holder":
        scene["props"][0]["visible"] = False
    elif case == "unknown_holder":
        scene["props"][0]["holder"] = "missing"
    if case in {"valid", "invisible_holder"}:
        assert StoryPlan.model_validate(value).title == "Sample"
    else:
        with pytest.raises(ValueError):
            StoryPlan.model_validate(value)


def test_archive_render_and_images_retain_original_placement(tmp_path, session):
    from komaquill.images.generation import new_run

    client = Client.initialize(tmp_path)
    identity, folder = retained(client)
    project = find_project(tmp_path)
    plan = client.archive.get(identity)["plan"]
    run_id, render_id = uuid4(), uuid4()
    render_folder = folder / "renders" / str(render_id)
    render = StoryRender(
        story_id=identity,
        render_id=render_id,
        plan_sha256=digest(plan),
        image_profile="saved",
        settings={},
        scenes=[{"scene_id": plan["scenes"][0]["scene_id"], "seed": 1, "run_ids": [str(run_id)]}],
    )
    atomic_json(render_folder / "render.json", render.model_dump(mode="json"))
    index = Locations(project).read().model_dump(mode="json")
    index["runs"][str(run_id)] = {
        "folder": f"{render_folder.relative_to(project.data).as_posix()}/runs/{run_id}",
        "images": f"{render_folder.relative_to(project.data).as_posix()}/images",
        "scene": 1,
        "attempt": 1,
        "recorded": True,
    }
    atomic_json(Locations(project).index_file, index)
    run = new_run(project, session, run_id, {})
    run.state = "saved"
    atomic_json(render_folder / "runs" / str(run_id) / "run.json", run.model_dump(mode="json"))
    assert client.archive.get(identity)["renders"][0]["render_id"] == str(render_id)
    assert client.archive.list()["images"][0]["run_id"] == str(run_id)
    assert client.archive.get(run_id)["kind"] == "image"
    assert client.archive.artifact_path(f"runs/{run_id}/images/0001.png").name == "001-001-001.png"
    with pytest.raises(ValueError):
        StoryRender.model_validate({**render.model_dump(), "format_version": 2})
    with pytest.raises(ValueError):
        StoryInput.model_validate({**plan["request"], "source_sha256": "changed"})
