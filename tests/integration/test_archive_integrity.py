"""Keep corrupt retained records isolated and reject unsafe placement metadata."""

import json
from uuid import uuid4

import pytest
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.locations import Locations
from komaquill.adapters.workspace import find_project
from komaquill.api import Client, KomaquillError
from komaquill.domain.workflow import digest

from tests.integration.test_archive import retained


@pytest.mark.parametrize(
    "case",
    [
        "request_id",
        "request_missing",
        "checksum",
        "plan_request",
        "plan_missing",
        "index_invalid",
        "index_traversal",
        "migration_pending",
        "migration_invalid",
        "orphan",
        "run_invalid",
        "run_id",
    ],
)
def test_archive_rejects_corruption_without_rewriting(tmp_path, case, session):
    api = Client.initialize(tmp_path)
    identity, folder = retained(api)
    project = find_project(tmp_path)
    locations = Locations(project)
    if case == "request_id":
        value = json.loads((folder / "request.json").read_bytes())
        value["story_id"] = str(uuid4())
        atomic_json(folder / "request.json", value)
    elif case == "request_missing":
        (folder / "request.json").unlink()
    elif case in {"checksum", "plan_request"}:
        value = json.loads((folder / "plan.json").read_bytes())
        value["plan"]["request"]["purpose"] = "changed"
        if case == "plan_request":
            value["sha256"] = digest(value["plan"])
        atomic_json(folder / "plan.json", value)
    elif case == "plan_missing":
        (folder / "plan.json").unlink()
    elif case == "index_invalid":
        locations.index_file.write_text("invalid")
    elif case == "index_traversal":
        atomic_json(locations.index_file, {"stories": {str(identity): "../escape"}})
    elif case.startswith("migration_"):
        atomic_json(
            locations.migration_file,
            {"status": "running"} if case.endswith("pending") else {"invalid": True},
        )
    elif case == "orphan":
        locations.index_file.unlink()
    elif case in {"run_invalid", "run_id"}:
        from komaquill.images.generation import new_run

        run_id = uuid4()
        value = (
            new_run(project, session, uuid4(), {}).model_dump(mode="json")
            if case == "run_id"
            else {"invalid": True}
        )
        atomic_json(project.runs / str(run_id) / "run.json", value)
        index = locations.read().model_dump(mode="json")
        index["runs"][str(run_id)] = {"folder": f"runs/{run_id}", "recorded": True}
        atomic_json(locations.index_file, index)
    before = {p: p.read_bytes() for p in project.data.rglob("*") if p.is_file()}
    if case in {"run_invalid", "run_id"}:
        assert api.archive.list()["unreadable_ids"]
    else:
        with pytest.raises(KomaquillError):
            api.archive.get(identity)
    assert before == {p: p.read_bytes() for p in project.data.rglob("*") if p.is_file()}
