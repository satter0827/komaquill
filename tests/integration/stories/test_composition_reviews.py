"""Keep saved image evidence and append-only quality decisions separate."""

import json
from uuid import uuid4

from typer.testing import CliRunner

from komaquill.api import Client
from komaquill.application.evaluations import register
from komaquill.domain.evaluations import Evaluation
from komaquill.infrastructure.evaluations import FileEvaluationRepository
from komaquill.infrastructure.stories.publication import publish_story
from komaquill.interfaces.cli import app
from tests.integration.stories.test_publication import rendered


def test_reviews_reach_api_manifest_markdown_and_export_without_changing_storage(
    project, session, client, monkeypatch
):
    repository, plan, render, _ = rendered(project, session, client)
    run = project.repository.load()[0]
    review = Evaluation(
        run_id=run.run_id,
        image=run.images[0],
        criterion="contact",
        decision="reject",
        reason="The umbrella floats.",
    )
    register(project.repository, FileEvaluationRepository(project), [review])
    publish_story(repository, project, plan.request.story_id)
    api = Client(project.root)
    snapshot = api.stories.show(plan.request.story_id)
    assert snapshot.complete and snapshot.finished
    assert [row.evaluation_id for row in snapshot.evaluations] == [review.evaluation_id]
    folder = repository.folder(plan.request.story_id) / "renders" / str(render.render_id)
    markdown = (folder / "story.md").read_text()
    assert "reject" in markdown and review.reason in markdown
    assert (
        json.loads((folder / "manifest.json").read_bytes())["evaluations"][0]["decision"]
        == "reject"
    )
    for detail in ("summary", "full"):
        exported = api.stories.export(plan.request.story_id, detail=detail)
        assert [row.evaluation_id for row in exported.evaluations] == [review.evaluation_id]
    assert project.repository.read(run.run_id).state == "saved"
    (project.evaluations / f"{uuid4()}.json").write_text("{}")
    snapshot = api.stories.show(plan.request.story_id)
    assert len(snapshot.unreadable_evaluation_ids) == 1 and snapshot.complete
    monkeypatch.setenv("KOMAQUILL_PROJECT_DIR", str(project.root))
    result = CliRunner().invoke(app, ["stories", "show", str(plan.request.story_id)])
    assert "Some reviews could not be read" in result.output


def test_duplicate_candidates_are_identified_without_discarding_any_image(project, session, client):
    from komaquill.application.generation import collect, submit_generation

    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a cat", None, 7, candidates=2)
    collect(project, client, active)
    saved = project.repository.read(run.run_id)
    first, second = saved.images
    assert saved.duplicate_candidates == {second: first}
    saved.image_metadata[first].role = "auxiliary"
    assert saved.duplicate_candidates == {}


def test_material_review_is_idempotent_and_corrections_are_append_only(project, session, client):
    from tests.live.composition import review

    rendered(project, session, client)
    run = project.repository.load()[0]
    first = review(project.root, run.run_id, run.images[0], "reject", "Floating handle.", "grip")
    assert (
        review(project.root, run.run_id, run.images[0], "reject", "Floating handle.", "grip")
        == first
    )
    correction = review(
        project.root, run.run_id, run.images[0], "undecided", "Occluded grip.", "grip"
    )
    assert correction.supersedes == first.evaluation_id
    assert len(Client(project.root).evaluations.list(run.run_id)) == 2


def test_private_material_checks_refuse_unsafe_resubmission(project, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from tests.live import composition

    def unexpected(*args, **kwargs):
        raise AssertionError("Preconditions must fail before any image API is used.")

    monkeypatch.setattr(composition, "Client", unexpected)
    report = project.root / "report"
    destination = report / "materials"
    destination.mkdir(parents=True)
    project.state.mkdir(parents=True)
    marker = project.state / "monitor.json"
    import pytest

    def attempt():
        with pytest.raises(RuntimeError):
            composition.material(project.root, report, "pair-a")

    (destination / "pair-a.json").write_text("{}")
    attempt()
    (destination / "pair-a.json").unlink()
    marker.write_text('{"phase":"starting"}')
    attempt()
    marker.write_text(json.dumps({"phase": "ready", "created_at": "2020-01-01T00:00:00Z"}))
    attempt()
    marker.write_text(
        json.dumps(
            {"phase": "ready", "created_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
        )
    )
    for index in range(composition.MAX_MATERIAL_CASES):
        (destination / f"{index}.json").write_text("{}")
    attempt()
