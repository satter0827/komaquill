"""Verify selective publication, external writers and restart reconstruction."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from komaquill.application.generation import collect
from komaquill.application.stories.rendering import create_render, render_story
from komaquill.infrastructure.locations import Locations
from komaquill.infrastructure.stories import publication
from tests.support.stories import planned


def rendered(project, session, client):
    repository, plan = planned(project, 1)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "klein", 1)
    render_story(repository, project, client, active, render.render_id)
    return repository, plan, repository.render(render.render_id), active


def test_unchanged_observations_do_not_publish_or_rewrite(project, session, client, monkeypatch):
    repository, plan, render, active = rendered(project, session, client)
    tracker = publication.PublicationTracker(project)
    assert tracker.reconcile() == ()
    versions = collect(project, client, active).observed_versions
    assert tracker.tick(versions) == ()
    files = [p for p in repository.folder(plan.request.story_id).rglob("*") if p.is_file()]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    monkeypatch.setattr(publication, "publish_story", lambda *a: pytest.fail("Unchanged work"))
    monkeypatch.setattr(publication, "publish_catalog", lambda *a: pytest.fail("Unchanged catalog"))
    assert tracker.tick(versions) == ()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}


def test_publication_retries_without_recollecting_and_survives_lost_memory(
    project, session, client, monkeypatch
):
    repository, plan, render, active = rendered(project, session, client)
    tracker = publication.PublicationTracker(project)
    actual = publication.atomic_json
    monkeypatch.setattr(
        publication, "atomic_json", lambda *a: (_ for _ in ()).throw(OSError("private-write-error"))
    )
    assert tracker.reconcile() == (plan.request.story_id,)
    log = project.logs / "cli.jsonl"
    before = log.read_bytes()
    assert tracker.tick(()) == (plan.request.story_id,)
    assert log.read_bytes() == before
    assert project.repository.load()[0].state == "saved"
    monkeypatch.setattr(publication, "atomic_json", actual)
    assert tracker.tick(()) == ()
    manifest = (
        repository.folder(plan.request.story_id)
        / "renders"
        / str(render.render_id)
        / "manifest.json"
    )
    manifest.unlink()
    assert publication.PublicationTracker(project).reconcile() == ()
    assert manifest.is_file()


def test_a_second_writer_is_detected_when_this_collection_saves_nothing(
    project, session, client, monkeypatch
):
    repository, plan, render, active = rendered(project, session, client)
    tracker = publication.PublicationTracker(project)
    tracker.reconcile()
    tracker.tick(collect(project, client, active).observed_versions)
    # A separate adapter changes a recorded failure; the observer owns no local writes.
    from komaquill.domain.errors import KomaquillError, failure_info
    from komaquill.infrastructure.storage import FileRunRepository

    other = FileRunRepository(project)
    run = other.load()[0]
    run.failure = failure_info(
        KomaquillError(code="publication_failed", stage="finalize"), "finalize"
    )
    other.save(run)
    observation = collect(project, client, active)
    assert observation.saved == 0
    calls = []
    actual = publication.publish_story

    def publish(*args):
        calls.append(args[2:])
        return actual(*args)

    monkeypatch.setattr(publication, "publish_story", publish)
    tracker.tick(observation.observed_versions)
    assert calls == [(plan.request.story_id, render.render_id)]


def test_storage_snapshot_is_bounded_and_mutations_reread_the_index(project, monkeypatch):
    locations = Locations(project)
    locations.ensure()
    original = Path.read_bytes
    reads = []

    def read(path):
        if path == locations.index_file:
            reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with locations.snapshot():
        for _ in range(5):
            locations.read()
        assert len(reads) == 1
        identity = uuid4()
        locations.register_run(identity)
        assert identity in locations.read().runs
    assert identity in locations.read().runs
    assert len(reads) >= 3


def test_only_changed_story_is_published(project, session, client, monkeypatch):
    first = rendered(project, session, client)
    second = rendered(project, session, client)
    tracker = publication.PublicationTracker(project)
    tracker.reconcile()
    tracker.tick(collect(project, client, first[3]).observed_versions)
    # Observe one new run revision without inventing an additional GPU request.
    revision = project.repository.read(first[2].scenes[0].run_ids[0])
    revision.generation_completed_at = revision.created_at
    project.repository.save(revision)
    calls = []
    actual = publication.publish_story
    monkeypatch.setattr(
        publication, "publish_story", lambda *a: (calls.append(a[2:]), actual(*a))[-1]
    )
    tracker.tick(collect(project, client, first[3]).observed_versions)
    assert calls == [(first[1].request.story_id, first[2].render_id)]
    assert second[2].render_id not in [call[1] for call in calls]


def test_association_failure_and_recovery_keep_request_identity(project, monkeypatch):
    tracker = publication.PublicationTracker(project)
    run_id = uuid4()
    monkeypatch.setattr(
        tracker.repository,
        "render_for_run",
        lambda identity: (_ for _ in ()).throw(OSError("private-association-error")),
    )
    assert tracker.tick(((run_id, "revision"),)) == (run_id,)
    assert tracker.tick(((run_id, "revision"),)) == (run_id,)
    monkeypatch.setattr(tracker.repository, "render_for_run", lambda identity: None)
    assert tracker.tick(()) == ()
    before = (project.logs / "cli.jsonl").read_bytes()
    assert tracker.tick(()) == ()
    assert (project.logs / "cli.jsonl").read_bytes() == before
    events = [json.loads(line) for line in before.splitlines()]
    assert [event["result"] for event in events] == ["failed", "recovered"]
    assert all(event["run_id"] == str(run_id) and event["story_id"] is None for event in events)
    assert "private-association-error" not in before.decode()


def test_successful_render_does_not_clear_another_render_failure(project, monkeypatch):
    tracker = publication.PublicationTracker(project)
    story_id, failed_render, saved_render = uuid4(), uuid4(), uuid4()
    tracker.pending.update({(story_id, failed_render), (story_id, saved_render)})

    def publish(repository, workspace, identity, render_id):
        if render_id == failed_render:
            raise OSError("private-render-error")

    monkeypatch.setattr(publication, "publish_story", publish)
    monkeypatch.setattr(publication, "publish_catalog", lambda *args: None)
    assert tracker.tick(()) == (story_id,)
    before = (project.logs / "cli.jsonl").read_bytes()
    assert tracker.tick(()) == (story_id,)
    assert (project.logs / "cli.jsonl").read_bytes() == before
    monkeypatch.setattr(publication, "publish_story", lambda *args: None)
    assert tracker.tick(()) == ()
    events = [json.loads(line) for line in (project.logs / "cli.jsonl").read_bytes().splitlines()]
    assert [event["result"] for event in events] == ["failed", "recovered"]
    assert all(event["story_id"] == str(story_id) for event in events)
    assert all(event["render_id"] == str(failed_render) for event in events)
