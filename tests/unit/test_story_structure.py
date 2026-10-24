"""Keep local planning out of GPU execution and protect evidence boundaries."""

import ast
import inspect
import json
from datetime import datetime

import pytest

from komaquill.application.stories.rendering import create_render, render_story
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import evaluations, provenance
from komaquill.interfaces.cli import start
from tests.support.stories import planned


def test_story_resources_and_clients_are_local_only():
    entries = dict(provenance.gpu_contents())
    assert not any("/stories/" in name for name in entries)
    for name, content in entries.items():
        if not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(content)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                value.startswith(("langchain", "langsmith", "openai")) for value in names
            )


def test_start_only_opens_a_browser_when_explicitly_requested():
    assert inspect.signature(start).parameters["open_browser"].default is False


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
