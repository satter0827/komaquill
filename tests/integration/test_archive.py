"""Verify retained plans are checked and read without modifying their files."""

import hashlib
from uuid import uuid4

import pytest
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.locations import Locations
from komaquill.adapters.workspace import find_project
from komaquill.api import Client, KomaquillError
from komaquill.domain.workflow import digest


def retained(client):
    project = find_project(client.workspace)
    from komaquill.archive.models import PlannerProvenance, Scene, StoryInput, StoryPlan

    source = "A quiet room."
    request = StoryInput(
        source_name="sample.txt",
        source_text=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        image_count=1,
    )
    folder = project.data / "stories" / str(request.story_id)
    atomic_json(
        Locations(project).index_file,
        {
            "format_version": 1,
            "stories": {str(request.story_id): folder.relative_to(project.data).as_posix()},
            "runs": {},
        },
    )
    atomic_json(folder / "request.json", request.model_dump(mode="json"))
    plan = StoryPlan(
        request=request,
        title="Sample",
        synopsis=source,
        characters=[],
        supplements=[],
        scenes=[
            Scene(
                title="Room",
                narrative=source,
                visual_description="an empty room",
                location=None,
                source_refs=["p1"],
                characters=[],
                scene_id=uuid4(),
                position=1,
            )
        ],
        planner=PlannerProvenance(
            profile="saved",
            provider="fake",
            configured_model="saved",
            instructions="saved instructions",
            instruction_sha256="a" * 64,
            timeout_seconds=1,
            max_output_tokens=10,
        ),
    )
    value = plan.model_dump(mode="json")
    atomic_json(folder / "plan.json", {"format_version": 1, "plan": value, "sha256": digest(value)})
    return request.story_id, folder


def test_archive_reads_and_exports_without_rewriting_the_original(tmp_path):
    client = Client.initialize(tmp_path)
    identity, folder = retained(client)
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    assert client.archive.list()["works"][0]["id"] == str(identity)
    assert client.archive.get(identity)["plan"]["title"] == "Sample"
    destination = tmp_path / ".artifacts/records/export.json"
    client.archive.export(identity, destination)
    with pytest.raises(KomaquillError):
        client.archive.export(identity, destination)
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before


def test_corrupt_archive_is_not_repaired_or_reclassified_as_unsent(tmp_path):
    client = Client.initialize(tmp_path)
    identity, folder = retained(client)
    (folder / "plan.json").write_text('{"private":"invalid"}')
    original = (folder / "plan.json").read_bytes()
    with pytest.raises(KomaquillError) as error:
        client.archive.get(identity)
    assert error.value.code == "storage_failed"
    assert (folder / "plan.json").read_bytes() == original
