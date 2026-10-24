"""Verify observations and story associations without rewriting source evidence."""

import json
from datetime import datetime

import pytest

from komaquill.application.stories.rendering import create_render, render_story
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import evaluations
from tests.support.stories import planned


def test_export_has_observation_times_and_story_context(project, session, client):
    repository, plan = planned(project, 1)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    render_story(repository, project, client, active, render.render_id)
    value = evaluations.export_evidence(project)
    assert datetime.fromisoformat(value["exported_at"]).tzinfo is not None
    assert value["exporter"]["code_sha256"]
    row = value["runs"][0]
    assert row["story_context"][0]["scene_id"] == str(plan.scenes[0].scene_id)
    assert row["story_context"][0]["planner_provider"] == "fake"
    assert datetime.fromisoformat(row["images"][0]["observed"]["observed_at"]).tzinfo is not None
    path = (
        repository.folder(plan.request.story_id) / "renders" / str(render.render_id) / "render.json"
    )
    path.write_text("{", encoding="utf-8")
    value = evaluations.export_evidence(project)
    assert len(value["runs"]) == 1 and value["unreadable_story_renders"] == [str(render.render_id)]


def test_tampered_plan_is_not_used_for_association_export(project, session, client):
    repository, plan = planned(project, 1)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    render_story(repository, project, client, active, render.render_id)
    path = (
        repository.folder(plan.request.story_id) / "renders" / str(render.render_id) / "render.json"
    )
    value = json.loads(path.read_bytes())
    value["plan_sha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    exported = evaluations.export_evidence(project)
    assert exported["unreadable_story_renders"] == [str(render.render_id)]
    with pytest.raises(KomaquillError):
        from komaquill.application.stories.rendering import story_view

        story_view(repository, project, plan.request.story_id, render.render_id)


def test_failed_fake_planning_remains_in_comparison_evidence(project):
    import hashlib
    from datetime import UTC
    from uuid import UUID

    from komaquill.api import Client
    from tests.support.stories import source
    from tests.support.story_evidence import planning_measurement

    api = Client(project.root)
    with pytest.raises(KomaquillError) as failure:
        api.stories.plan(source() + " changed", llm_profile="fake")
    identity = UUID(failure.value.details["story_id"])
    result = planning_measurement(
        api,
        identity,
        case_id="changed",
        count="auto",
        source_sha256=hashlib.sha256((source() + " changed").encode()).hexdigest(),
        started_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=1,
        failure=failure.value,
    )
    assert result["status"] == "failed" and result["calls"] == 1
    assert result["total_tokens"] is None and result["actual_images"] is None
    assert result["evidence"]["planning"]["attempts"][0]["failure"]["code"] == "fake_story_mismatch"
