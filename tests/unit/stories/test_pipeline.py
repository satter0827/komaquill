"""Exercise bounded submission, safe resumption and visible candidate checkpoints."""

from datetime import UTC, datetime, timedelta

import pytest

from komaquill.application.generation import collect
from komaquill.application.stories.rendering import create_render, render_story, story_view
from komaquill.domain.errors import KomaquillError
from tests.support.stories import planned


def pipeline(project, session, count=4):
    repository, plan = planned(project, count)
    settings = project.settings().model_copy(update={"story_max_in_flight": 2})
    active = session.model_copy(update={"settings": settings})
    render = create_render(
        repository,
        project,
        active,
        plan.request.story_id,
        "pony",
        42,
        candidates=2,
        negative="extra umbrellas",
    )
    return repository, active, render


def test_two_batches_are_submitted_before_first_collection_and_never_more(
    project, session, client, stub, monkeypatch
):
    repository, active, render = pipeline(project, session)
    submission_counts = []
    original = client.history

    def history():
        submission_counts.append(stub.submissions)
        return original()

    monkeypatch.setattr(client, "history", history)
    views = []
    view = render_story(repository, project, client, active, render.render_id, notify=views.append)
    assert view["complete"] and stub.submissions == 4
    assert submission_counts == [2, 4]
    assert max(v["counts"].get("queued", 0) for v in views) == 2
    assert all(len(scene["candidates"]) == 2 for scene in view["scenes"])
    assert all(
        scene["compiled_input"]["requested_negative"] == "extra umbrellas"
        for scene in view["scenes"]
    )
    assert all(scene["requested_candidates"] == 2 for scene in view["scenes"])


def test_partial_candidate_is_visible_while_batch_remains_pending(project, session, client, stub):
    repository, active, render = pipeline(project, session, 1)

    def interrupt(view):
        if view["counts"].get("queued"):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        render_story(repository, project, client, active, render.render_id, notify=interrupt)
    stub.invalid_images.add("candidate-1.png")
    collect(project, client, active)
    view = story_view(repository, project, render.story_id, render.render_id)
    assert not view["finished"] and len(view["scenes"][0]["candidates"]) == 1
    assert view["scenes"][0]["primary_image"] is not None
    stub.invalid_images.clear()
    assert render_story(repository, project, client, active, render.render_id)["complete"]
    assert stub.submissions == 1


def test_unknown_submission_halts_pipeline_and_resume_reconciles_before_new_posts(
    project, session, client, stub
):
    repository, active, render = pipeline(project, session)
    stub.drop_response = True
    with pytest.raises(KomaquillError):
        render_story(repository, project, client, active, render.render_id)
    assert stub.submissions == 1
    stub.drop_response = False
    assert render_story(repository, project, client, active, render.render_id)["complete"]
    assert stub.submissions == 4


def test_collection_failure_stops_at_the_bound_and_retry_reuses_saved_conditions(
    project, session, client, stub
):
    repository, active, render = pipeline(project, session)
    stub.corrupt_image = True
    with pytest.raises(KomaquillError) as error:
        render_story(repository, project, client, active, render.render_id)
    assert error.value.code == "collection_failed" and stub.submissions == 2
    stub.corrupt_image = False
    changed = active.model_copy(
        update={"settings": active.settings.model_copy(update={"story_max_in_flight": 1})}
    )
    view = render_story(repository, project, client, changed, render.render_id)
    assert view["complete"] and all(len(scene["candidates"]) == 2 for scene in view["scenes"])


def test_resumption_does_not_restart_the_request_timeout(project, session, client, stub):
    repository, active, render = pipeline(project, session)

    def interrupt(view):
        if view["counts"].get("queued"):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        render_story(repository, project, client, active, render.render_id, notify=interrupt)
    run = project.repository.load()[0]
    run.created_at = datetime.now(UTC) - timedelta(seconds=10)
    project.repository.save(run)
    stub.history.clear()
    with pytest.raises(KomaquillError) as error:
        render_story(repository, project, client, active, render.render_id)
    assert error.value.code == "generation_timeout" and stub.submissions == 1
