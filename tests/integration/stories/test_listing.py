"""List complete, incomplete and unplanned works without model connections."""

import json

import pytest
from typer.testing import CliRunner

from komaquill.api import Client, KomaquillError
from komaquill.interfaces.cli import app
from tests.support.stories import source


def test_listing_keeps_failed_work_and_healthy_plan_visible(project):
    api = Client(project.root)
    plan = api.stories.plan(source(), llm_profile="fake")
    with pytest.raises(KomaquillError):
        api.stories.plan(source() + " changed", llm_profile="fake")
    listing = api.stories.list()
    assert {row.state for row in listing.stories} == {"unplanned", "planned"}
    assert not listing.unreadable_ids
    root = project.data / "stories"
    index = (root / "README.md").read_text(encoding="utf-8")
    assert plan.title in index and "unplanned" in index
    result = CliRunner().invoke(app, ["--project", str(project.root), "stories", "list", "--json"])
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)["stories"]) == 2
    request = next(
        row
        for row in root.glob("*/request.json")
        if row.parent.name.endswith(str(plan.request.story_id)[:8])
    )
    request.write_text("{", encoding="utf-8")
    listing = api.stories.list()
    assert len(listing.stories) == 1 and listing.unreadable_ids == [plan.request.story_id]
    result = CliRunner().invoke(app, ["--project", str(project.root), "stories", "list"])
    assert result.exit_code == 1
