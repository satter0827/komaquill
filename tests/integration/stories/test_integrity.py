"""Reject incomplete or inconsistent originals without rewriting retained evidence."""

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest

from komaquill.api import Client, KomaquillError
from komaquill.application.generation import collect
from komaquill.application.stories.rendering import create_render, render_story
from komaquill.infrastructure.storage import FileRunRepository
from tests.support.stories import planned, source


def rendered(project, session, client):
    repository, plan = planned(project, 1)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "klein-4b", 1)
    render_story(repository, project, client, active, render.render_id)
    return repository, plan, render, active


def test_successful_plan_cannot_be_reclassified_as_unplanned_after_loss(project):
    api = Client(project.root)
    plan = api.stories.plan(source(), llm_profile="fake", images=1)
    (api.stories._repository.folder(plan.request.story_id) / "plan.json").unlink()
    with pytest.raises(KomaquillError) as error:
        api.stories.export(plan.request.story_id)
    assert error.value.code == "storage_failed"
    assert error.value.details["story_id"] == str(plan.request.story_id)
    listing = api.stories.list()
    assert listing.stories == [] and listing.unreadable_ids == [plan.request.story_id]


@pytest.mark.parametrize("change", ["identity", "source"])
def test_request_and_plan_must_describe_the_same_original(project, change):
    api = Client(project.root)
    plan = api.stories.plan(source(), llm_profile="fake", images=1)
    path = api.stories._repository.folder(plan.request.story_id) / "request.json"
    request = json.loads(path.read_bytes())
    if change == "identity":
        request["story_id"] = str(uuid4())
    else:
        request["source_text"] = "Different retained original."
        request["source_sha256"] = hashlib.sha256(request["source_text"].encode()).hexdigest()
    path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(KomaquillError) as error:
        api.stories.export(plan.request.story_id)
    assert error.value.code == "storage_failed"
    assert error.value.details["story_id"] == str(plan.request.story_id)


def test_export_does_not_silently_discard_a_lost_earlier_attempt(
    project, session, client, stub, monkeypatch
):
    original = client.submit

    def submit(*args):
        prompt_id = original(*args)
        if stub.submissions == 1:
            stub.history[prompt_id]["status"] = {"status_str": "error", "completed": True}
        return prompt_id

    monkeypatch.setattr(client, "submit", submit)
    repository, plan, render, active = rendered(project, session, client)
    render_story(repository, project, client, active, render.render_id, retry_failed=True)
    prior = repository.render(render.render_id).scenes[0].run_ids[0]
    api = Client(project.root)
    api.artifact_path(f"runs/{prior}/run.json").unlink()
    with pytest.raises(KomaquillError) as error:
        api.stories.export(plan.request.story_id)
    assert error.value.code == "storage_failed"
    assert error.value.details["run_id"] == str(prior)


@pytest.mark.parametrize("graph", [[{}], {"3": {"class_type": "KSampler"}}])
def test_malformed_saved_workflow_raises_a_safe_storage_error(project, session, client, graph):
    repository, plan, render, active = rendered(project, session, client)
    run_id = repository.render(render.render_id).scenes[0].run_ids[0]
    api = Client(project.root)
    api.artifact_path(f"runs/{run_id}/workflow-api.json").write_text(
        json.dumps(graph), encoding="utf-8"
    )
    with pytest.raises(KomaquillError) as error:
        api.stories.export(plan.request.story_id)
    assert error.value.code == "storage_failed"
    assert error.value.details["run_id"] == str(run_id)


def test_final_collection_observation_reports_newly_unreadable_history(
    project, client, session, monkeypatch
):
    graph, ui = project.prepare("a lake", 1, "ui-request")
    client.submit(graph, ui, "ui-request")
    original = FileRunRepository.load
    calls = 0
    damaged = []

    def load(repository, unreadable=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            run = original(repository)[0]
            damaged.append(run.run_id)
            Client(project.root).artifact_path(f"runs/{run.run_id}/run.json").write_text("null")
        return original(repository, unreadable)

    monkeypatch.setattr(FileRunRepository, "load", load)
    result = collect(project, client, session)
    assert result.errors == tuple(damaged)


def test_readable_output_failure_does_not_stop_later_scenes(
    project, session, client, stub, monkeypatch, capsys
):
    import typer

    from komaquill.api import stories
    from komaquill.infrastructure.stories import publication
    from komaquill.interfaces.stories import present

    repository, plan = planned(project, 4)
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    original = publication.atomic_json

    def write(path, value):
        if path.name == "manifest.json":
            raise OSError("private-publication-error")
        return original(path, value)

    monkeypatch.setattr(publication, "atomic_json", write)
    api = Client(project.root)
    events = list(api.stories.render(plan.request.story_id))
    assert events[-1].snapshot.complete and events[-1].kind == "finished"
    assert events[-1].publication_failure is not None
    assert stub.submissions == 4
    with pytest.raises(typer.Exit) as error:
        present(api, iter(events), True)
    assert error.value.exit_code == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["complete"]
    assert "--refresh" in output.err and "private-publication-error" not in output.err
    monkeypatch.setattr(publication, "atomic_json", original)
    resumed = list(api.stories.resume(events[-1].snapshot.render_id))
    assert resumed[-1].publication_failure is None and stub.submissions == 4


def test_projection_reconciliation_reports_a_missing_completed_plan(project):
    from komaquill.infrastructure.stories.publication import refresh_saved_stories

    api = Client(project.root)
    plan = api.stories.plan(source(), llm_profile="fake", images=1)
    (api.stories._repository.folder(plan.request.story_id) / "plan.json").unlink()
    assert refresh_saved_stories(project) == (plan.request.story_id,)


def test_unrelated_broken_model_metadata_does_not_block_story_summary(project):
    api = Client(project.root)
    plan = api.stories.plan(source(), llm_profile="fake", images=1)
    path = project.models / "checkpoints" / ("0" * 64) / "asset.json"
    path.parent.mkdir(parents=True)
    path.write_text("null", encoding="utf-8")
    assert api.stories.export(plan.request.story_id).planning.status == "succeeded"


def test_referenced_broken_model_metadata_is_a_safe_failure(project, session, client):
    repository, plan, render, active = rendered(project, session, client)
    run_id = repository.render(render.render_id).scenes[0].run_ids[0]
    run = project.repository.read(run_id)
    model = next(iter(run.asset_sha256.values()))
    path = project.models / "diffusion_models" / model / "asset.json"
    path.parent.mkdir(parents=True)
    path.write_text("null", encoding="utf-8")
    with pytest.raises(KomaquillError) as error:
        Client(project.root).stories.export(plan.request.story_id)
    assert error.value.code == "storage_failed"


def test_corrupt_optional_evaluation_is_reported_without_hiding_a_story(project):
    api = Client(project.root)
    plan = api.stories.plan(source(), llm_profile="fake", images=1)
    identity = uuid4()
    project.evaluations.mkdir(parents=True)
    (project.evaluations / f"{identity}.json").write_text("null", encoding="utf-8")
    evidence = api.stories.export(plan.request.story_id)
    assert evidence.story is not None
    assert evidence.unreadable_evaluation_ids == [str(identity)]


def test_unplanned_corrupt_request_is_reported_by_projection_reconciliation(project):
    from komaquill.infrastructure.stories.publication import refresh_saved_stories
    from tests.support.stories import request

    api = Client(project.root)
    pending = request(1)
    api.stories._repository.begin(pending)
    (api.stories._repository.folder(pending.story_id) / "request.json").write_text("null")
    assert refresh_saved_stories(project) == (pending.story_id,)


def test_evaluation_file_identity_is_checked_before_association(project):
    from komaquill.domain.evaluations import Evaluation
    from komaquill.infrastructure.evaluations import FileEvaluationRepository

    record = Evaluation(run_id=uuid4(), criterion="content", reason="reviewed", decision="accept")
    project.evaluations.mkdir(parents=True)
    identity = uuid4()
    (project.evaluations / f"{identity}.json").write_text(
        record.model_dump_json(), encoding="utf-8"
    )
    repository = FileEvaluationRepository(project)
    with pytest.raises(KomaquillError) as error:
        repository.load()
    assert error.value.details["evaluation_id"] == str(identity)
    unreadable = []
    assert repository.load(unreadable) == [] and unreadable == [str(identity)]


def test_reserved_request_is_unsent_but_lost_placement_cannot_be_resubmitted(
    project, session, client, stub, monkeypatch
):
    from komaquill.api import stories
    from komaquill.infrastructure.locations import Locations

    repository, plan = planned(project, 1)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "klein-4b", 1)
    identity = uuid4()
    link = render.scenes[0].model_copy(update={"run_ids": [identity]})
    render = render.model_copy(update={"scenes": [link]})
    repository.save_render(render)
    api = Client(project.root)
    evidence = api.stories.export(plan.request.story_id)
    assert evidence.scenes[0].state == "unstarted" and evidence.selected_run_ids == [identity]
    assert evidence.runs == []
    locations = Locations(project)
    index = locations.read()
    index.runs.pop(identity)
    locations.save(index)

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    with pytest.raises(KomaquillError) as error:
        list(api.stories.resume(render.render_id))
    assert error.value.code == "storage_failed" and stub.submissions == 0
    assert error.value.details["run_id"] == str(identity)


@pytest.mark.parametrize("story_owned", [False, True])
def test_unindexed_recorded_history_is_not_overwritten(project, session, client, stub, story_owned):
    from komaquill.application.generation import generate
    from komaquill.infrastructure.locations import Locations

    if story_owned:
        repository, plan, render, active = rendered(project, session, client)
        identity = repository.render(render.render_id).scenes[0].run_ids[0]
    else:
        active = session
        identity = generate(project, client, session, "a lake", 1).run_id
    api = Client(project.root)
    path = api.artifact_path(f"runs/{identity}/run.json")
    before = path.read_bytes()
    locations = Locations(project)
    index = locations.read()
    index.runs.pop(identity)
    locations.save(index)
    result = collect(project, client, active)
    assert path.read_bytes() == before and stub.image_requests == 1
    assert identity in result.errors


@pytest.mark.parametrize("target", ["request", "render", "run", "migration-state"])
def test_rebuild_and_path_resolution_validate_identity_envelopes(project, session, client, target):
    from komaquill.infrastructure.locations import Locations

    repository, plan, render, active = rendered(project, session, client)
    if target == "request":
        path = repository.folder(plan.request.story_id) / "request.json"
    elif target == "render":
        path = (
            repository.folder(plan.request.story_id)
            / "renders"
            / str(render.render_id)
            / "render.json"
        )
    elif target == "run":
        identity = repository.render(render.render_id).scenes[0].run_ids[0]
        path = Client(project.root).artifact_path(f"runs/{identity}/run.json")
    else:
        path = Locations(project).migration_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises((KomaquillError, ValueError)):
        if target == "migration-state":
            Client(project.root).stories.list()
        else:
            Locations(project).rebuild()


def test_a_lost_story_index_entry_cannot_be_reported_as_no_stories(project):
    from komaquill.infrastructure.locations import Locations

    api = Client(project.root)
    api.stories.plan(source(), llm_profile="fake", images=1)
    locations = Locations(project)
    index = locations.read()
    index.stories.clear()
    locations.save(index)
    with pytest.raises(KomaquillError) as error:
        api.stories.list()
    assert error.value.code == "storage_failed" and error.value.details["record"] == "story-index"
