"""Keep saved image evidence and append-only quality decisions separate."""

import json
from datetime import timedelta
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
    run = submit_generation(project, client, active, "a cat", 7, candidates=2)
    collect(project, client, active)
    saved = project.repository.read(run.run_id)
    first, second = saved.images
    assert saved.duplicate_candidates == {second: first}
    saved.image_metadata[first].role = "auxiliary"
    assert saved.duplicate_candidates == {}


def test_review_corrections_are_append_only(project, session, client):
    from komaquill.api import Evaluator

    rendered(project, session, client)
    run = project.repository.load()[0]
    api = Client(project.root)
    first = Evaluation(
        run_id=run.run_id,
        image=run.images[0],
        criterion="grip",
        criterion_version="1",
        case_id="grip",
        decision="reject",
        reason="Floating handle.",
        evaluator=Evaluator(
            kind="ai", name="Fake review", model="fake", instruction="Assess physical contact."
        ),
    )
    api.evaluations.add([first])
    correction = first.model_copy(
        update={
            "evaluation_id": uuid4(),
            "created_at": first.created_at + timedelta(microseconds=1),
            "decision": "undecided",
            "reason": "Occluded grip.",
            "supersedes": first.evaluation_id,
        }
    )
    api.evaluations.add([correction])
    assert api.evaluations.list(run.run_id) == [first, correction]
    assert project.repository.read(run.run_id).state == "saved"
