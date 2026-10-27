"""Verify readable layout, resumable migration and reconstruction of storage placements."""

import json
from contextlib import contextmanager
from datetime import UTC
from uuid import uuid4

import pytest

from komaquill.api import Client, KomaquillError, stories
from komaquill.infrastructure import migration
from komaquill.infrastructure.locations import Locations, RunPlacement
from komaquill.infrastructure.stories import publication
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.support.storage import legacy_story
from tests.support.stories import source


def test_readable_images_are_ordered_and_the_only_binary_copy(
    project, session, client, monkeypatch
):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connected)
    api = Client(project.root)
    events = list(api.stories.generate(source(), llm_profile="fake", images=4))
    final = events[-1].snapshot
    repository = FileStoryRepository(project)
    root = repository.folder(final.story_id)
    assert root.name.startswith("202") and root.name != str(final.story_id)
    render = root / "renders" / str(final.render_id)
    assert sorted(p.name for p in (render / "images").iterdir()) == [
        f"{i:03d}-001-001.png" for i in range(1, 5)
    ]
    text = (render / "story.md").read_text(encoding="utf-8")
    assert all(scene.narrative in text for scene in final.scenes)
    manifest = json.loads((render / "manifest.json").read_bytes())
    assert manifest["complete"] and [s["position"] for s in manifest["scenes"]] == [1, 2, 3, 4]
    assert not list(project.runs.glob("*/images/*"))
    assert sum(1 for _ in project.data.rglob("*.png")) == 4


def test_migration_keeps_original_records_and_rebuilds_the_index(project, session, client):
    plan, render, originals = legacy_story(project, session, client)
    api = Client(project.root)
    with pytest.raises(KomaquillError) as failure:
        api.stories.show(plan.request.story_id)
    assert failure.value.code == "migration_required"
    preview = api.migrate(dry_run=True)
    assert preview.files > 0 and not Locations(project).index_file.exists()
    result = api.migrate()
    assert result.status == "complete"
    for (identity, name), content in originals.items():
        assert project.artifacts_store.read(f"runs/{identity}/{name}") == content
    assert api.stories.show(plan.request.story_id).complete
    assert not (project.data / "stories" / str(plan.request.story_id)).exists()
    before = Locations(project).read()
    Locations(project).index_file.unlink()
    api.migrate()
    assert Locations(project).read() == before
    assert str(api.stories.export(plan.request.story_id).story.render_id) == str(render.render_id)


def test_copy_interruption_leaves_sources_and_can_resume(project, session, client, monkeypatch):
    plan, render, originals = legacy_story(project, session, client)
    real = migration.shutil.copy2
    calls = []

    def interrupt(*args):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("not for diagnostics")
        return real(*args)

    monkeypatch.setattr(migration.shutil, "copy2", interrupt)
    with pytest.raises(KomaquillError):
        Client(project.root).migrate()
    assert (project.data / "stories" / str(plan.request.story_id) / "plan.json").exists()
    state = json.loads(Locations(project).migration_file.read_bytes())
    assert state["status"] == "copying" and not any(row["removed"] for row in state["moves"])
    monkeypatch.setattr(migration.shutil, "copy2", real)
    Client(project.root).migrate()
    assert Client(project.root).stories.show(plan.request.story_id).complete
    assert all(
        project.artifacts_store.read(f"runs/{identity}/{name}") == content
        for (identity, name), content in originals.items()
    )


def test_target_collision_is_not_overwritten(project, session, client):
    plan, render, originals = legacy_story(project, session, client)
    planned = migration.migration_plan(project)
    move = planned["moves"][0]
    target = Locations(project).path(move["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"different existing bytes")
    with pytest.raises(KomaquillError):
        Client(project.root).migrate()
    assert target.read_bytes() == b"different existing bytes"
    assert (project.data / "stories" / str(plan.request.story_id)).exists()


def test_owned_runtime_blocks_migration_without_provider_calls(project):
    project.state.mkdir(parents=True)
    (project.state / "colab.json").write_text("{}")
    with pytest.raises(KomaquillError):
        Client(project.root).migrate()
    assert not Locations(project).migration_file.exists()


def test_placement_validation_cannot_poison_an_existing_index(project):
    locations = Locations(project)
    locations.ensure()
    before = locations.index_file.read_bytes()
    with pytest.raises(KomaquillError):
        locations.register_run(uuid4(), RunPlacement(folder="../outside"))
    assert locations.index_file.read_bytes() == before


def test_publication_failure_preserves_saved_images_and_can_be_rebuilt(
    project, session, client, monkeypatch
):
    from komaquill.application.stories.rendering import create_render, render_story
    from tests.support.stories import planned

    repository, plan = planned(project, 1)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "klein-4b")
    render_story(repository, project, client, active, render.render_id)
    real = publication.atomic_json

    def fail(*args):
        raise OSError("private failure")

    monkeypatch.setattr(publication, "atomic_json", fail)
    with pytest.raises(KomaquillError) as error:
        Client(project.root).stories.refresh(plan.request.story_id)
    assert error.value.code == "publication_failed"
    assert all(run.state == "saved" for run in project.repository.load())
    monkeypatch.setattr(publication, "atomic_json", real)
    assert Client(project.root).stories.refresh(plan.request.story_id).complete


def test_markdown_cannot_embed_remote_images_or_injected_headings():
    text = publication.prose("![x](https://invalid.test/a)\n# heading <script>")
    assert "![x]" not in text and "\n# heading" not in text
    assert "<script>" not in text


def test_resume_after_original_file_deletion_fails(project, session, client, monkeypatch):
    from pathlib import Path

    plan, render, originals = legacy_story(project, session, client)
    real = Path.unlink
    failed = []

    def deny(path, *args, **kwargs):
        if path.name == "run.json" and path.parent.parent == project.runs and not failed:
            failed.append(path)
            raise PermissionError("source locked")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny)
    with pytest.raises(KomaquillError):
        Client(project.root).migrate()
    assert (
        json.loads(Locations(project).migration_file.read_bytes())["status"] == "removing_sources"
    )
    monkeypatch.setattr(Path, "unlink", real)
    Client(project.root).migrate()
    assert Client(project.root).stories.show(plan.request.story_id).complete
    for (identity, name), content in originals.items():
        assert project.artifacts_store.read(f"runs/{identity}/{name}") == content


def test_missing_save_time_metadata_is_observed_separately(project, session, client):
    plan, render, _ = legacy_story(project, session, client, count=1)
    run = next(project.runs.glob("*/run.json"))
    value = json.loads(run.read_bytes())
    value.pop("image_metadata")
    run.write_text(json.dumps(value), encoding="utf-8")
    original = run.read_bytes()
    Client(project.root).migrate()
    assert project.artifacts_store.read(f"runs/{value['run_id']}/run.json") == original
    report = next((project.reports / "migrations").glob("*-observations.json"))
    row = json.loads(report.read_bytes())["images"][0]
    assert row["metadata"] is None and row["observed"]["valid"]
    assert row["observed"]["width"] > 0 and row["observed"]["height"] > 0


def test_tampered_checkpoint_cannot_move_models(project):
    from datetime import datetime

    project.state.mkdir(parents=True)
    model = project.models / "protected.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"keep this model")
    state = {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "producer": {},
        "status": "copying",
        "index": {"format_version": 1, "stories": {}, "runs": {}},
        "moves": [
            {
                "source": "models/protected.bin",
                "target": "stories/x/model.bin",
                "temporary": "stories/x/tmp",
                "sha256": "0" * 64,
                "size_bytes": 15,
                "copied": False,
                "removed": False,
            }
        ],
    }
    Locations(project).migration_file.write_text(json.dumps(state))
    with pytest.raises(KomaquillError):
        Client(project.root).migrate()
    assert model.read_bytes() == b"keep this model"
    assert not (project.data / "stories").exists()


def test_concurrent_publication_cannot_replace_a_newer_snapshot_with_an_older_one(
    project, session, client, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from komaquill.application.stories.rendering import create_render, render_story
    from tests.support.stories import planned

    repository, plan = planned(project, 1)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "klein-4b")
    render_story(repository, project, client, active, render.render_id)
    actual = publication.story_view
    first_read = threading.Event()
    second_read = threading.Event()
    calls = []

    def read(*args):
        value = actual(*args)
        calls.append(True)
        if len(calls) == 1:
            first_read.set()
            second_read.wait(0.2)
            value["complete"] = False
            value["scenes"][0]["state"] = "queued"
            value["scenes"][0]["primary_image"] = None
        else:
            second_read.set()
        return value

    monkeypatch.setattr(publication, "story_view", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            publication.publish_story, repository, project, plan.request.story_id, render.render_id
        )
        assert first_read.wait(2)
        second = pool.submit(
            publication.publish_story, repository, project, plan.request.story_id, render.render_id
        )
        first.result()
        second.result()
    manifest = json.loads(
        (
            repository.folder(plan.request.story_id)
            / "renders"
            / str(render.render_id)
            / "manifest.json"
        ).read_bytes()
    )
    assert manifest["complete"] and manifest["scenes"][0]["state"] == "saved"


def test_catalog_repair_after_confirmed_gpu_stop_retains_ownership(project, session, client):
    from datetime import datetime

    from komaquill.domain.models import RuntimeObservation, ShutdownResult
    from komaquill.infrastructure.atomic import atomic_json

    plan, render, originals = legacy_story(project, session, client)
    owner = {
        "runtime_id": str(session.runtime_id),
        "endpoint_id": "endpoint",
        "account_sha256": "a" * 64,
    }
    atomic_json(project.state / "colab.json", owner)
    report = ShutdownResult(
        force=False,
        runtime_id=session.runtime_id,
        gpu="stopped",
        history="failed",
        finished_at=datetime.now(UTC),
        gpu_observation=RuntimeObservation(
            state="stopped", endpoint_id="endpoint", account_sha256="a" * 64
        ),
    )
    atomic_json(
        project.logs / str(session.runtime_id) / "shutdown.json", report.model_dump(mode="json")
    )
    result = Client(project.root).migrate()
    assert result.status == "complete" and (project.state / "colab.json").exists()
    assert Client(project.root).stories.show(plan.request.story_id).complete
    assert all(
        project.artifacts_store.read(f"runs/{identity}/{name}") == value
        for (identity, name), value in originals.items()
    )
    evaluation = {
        "format_version": 1,
        "evaluation_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "run_id": str(render.scenes[0].run_ids[0]),
        "criterion": "clarity",
        "reason": "Preserved scene.",
        "decision": "accept",
    }
    from komaquill.api import Evaluation

    api = Client(project.root)
    api.evaluations.add([Evaluation.model_validate(evaluation)])
    Locations(project).index_file.unlink()
    api.migrate()
    exported = api.evaluations.export()
    assert exported["evaluations"][0]["run_id"] == evaluation["run_id"]
    assert any(row["story_context"] for row in exported["runs"])
