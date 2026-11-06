"""Recover old competing image operations without submitting or weakening stale guards."""

import hashlib
import json
from uuid import uuid4

import pytest
from komaquill.adapters import image_link_repair as repair
from komaquill.adapters.persistence.schema import creation_jobs, images, presets
from komaquill.api import CharacterInput, Client, CreationRequest, KomaquillError, StoryInput
from komaquill.domain.library.models import CreationJob
from komaquill.domain.library.rendering import preset_image_input
from komaquill.domain.models import ImageMetadata, ImageRef, Run
from komaquill.interfaces.cli.main import app
from PIL import Image
from sqlalchemy import select
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def stopped_workspace(monkeypatch):
    monkeypatch.setattr(repair, "database_in_use", lambda project: False)


def retained(tmp_path, kind="story"):
    api = Client.initialize(tmp_path)
    target = (
        api.stories.create(StoryInput(name="Room", world="A quiet house"))
        if kind == "story"
        else api.characters.create(CharacterInput(name="Alex", age="60"))
    )
    store = api.library.store
    good_id, failed_id = uuid4(), uuid4()
    original = json.dumps(preset_image_input(kind, target))
    request = CreationRequest(
        operation_id=good_id,
        kind="banner" if kind == "story" else "portrait",
        target_id=target.id,
        expected_revision=target.revision,
    )
    good = CreationJob(id=good_id, request=request, state="submitted", original=original)
    failed = CreationJob(
        id=failed_id,
        request=request.model_copy(update={"operation_id": failed_id}),
        state="failed",
        error="operation_busy",
        original=original,
    )
    name = "images/0001.png"
    import io

    content = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(content, format="PNG")
    data = content.getvalue()
    api._project.artifacts_store.write(f"runs/{good_id}/{name}", data)
    path = api.artifact_path(f"runs/{good_id}/{name}")
    run = Run(
        run_id=good_id,
        runtime_id=uuid4(),
        state="saved",
        app_version="test",
        source_revision=None,
        remote_source_revision=None,
        comfy_revision=None,
        frontend_version=None,
        workflow_sha256="a" * 64,
        images=[name],
        image_metadata={
            name: ImageMetadata(
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                width=8,
                height=8,
                source=ImageRef(filename="image.png"),
            )
        },
    )
    with store.database.engine.begin() as connection:
        connection.execute(
            presets.update()
            .where(presets.c.id == str(target.id))
            .values(payload=target.model_copy(update={"image_job_id": failed_id}).model_dump_json())
        )
        for job in (good, failed):
            connection.execute(
                creation_jobs.insert().values(
                    id=str(job.id), payload=job.model_dump_json(), owner_pid=0, owner_started="0"
                )
            )
        connection.execute(images.insert().values(id=str(good_id), payload=run.model_dump_json()))
    return api, target, good, failed, run, path


def table(api, entity):
    with api.library.store.database.engine.connect() as connection:
        return connection.execute(select(entity)).fetchall()


@pytest.mark.parametrize("kind", ["character", "story"])
def test_inspect_is_read_only_and_apply_is_atomic_preserving_originals(tmp_path, kind):
    api, target, good, failed, _, path = retained(tmp_path, kind)
    before = {entity.name: table(api, entity) for entity in (images, creation_jobs, presets)}
    original = path.read_bytes()
    inspection = api.inspect_image_links()
    assert inspection.findings[0].reason == "ready"
    assert inspection.findings[0].candidates == [good.id]
    assert {
        entity.name: table(api, entity) for entity in (images, creation_jobs, presets)
    } == before
    result = api.repair_image_links(inspection.id)
    assert result.repaired == [target.id] and not result.skipped
    service = api.stories if kind == "story" else api.characters
    updated = service.get(target.id)
    assert updated.image_id == updated.image_job_id == good.id
    assert updated.revision == target.revision
    assert api.library.store.job(good.id)[0].state == "complete"
    assert api.library.store.job(failed.id)[0] == failed
    assert table(api, images) == before["images"] and path.read_bytes() == original
    backup = (
        tmp_path / ".artifacts/records/image-link-repairs" / str(inspection.id) / "before.sqlite3"
    )
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    again = Client(tmp_path).repair_image_links(inspection.id)
    assert again.already_applied == [target.id] and not again.repaired
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    "case", ["missing", "corrupt", "hash", "state", "ambiguous", "input", "revision"]
)
def test_unsafe_or_ambiguous_candidates_are_not_applied(tmp_path, case):
    api, target, good, _, run, path = retained(tmp_path)
    with api.library.store.database.engine.begin() as connection:
        if case == "missing":
            path.unlink()
        elif case == "corrupt":
            path.write_bytes(b"invalid")
        elif case == "hash":
            run.image_metadata[run.images[0]].sha256 = "0" * 64
            connection.execute(images.update().values(payload=run.model_dump_json()))
        elif case == "state":
            connection.execute(
                images.update().values(
                    payload=run.model_copy(update={"state": "queued"}).model_dump_json()
                )
            )
        elif case == "ambiguous":
            identity = uuid4()
            other = good.model_copy(
                update={
                    "id": identity,
                    "request": good.request.model_copy(update={"operation_id": identity}),
                }
            )
            connection.execute(
                creation_jobs.insert().values(
                    id=str(identity),
                    payload=other.model_dump_json(),
                    owner_pid=0,
                    owner_started="0",
                )
            )
        else:
            changed = (
                good.model_copy(update={"original": "{}"})
                if case == "input"
                else good.model_copy(
                    update={"request": good.request.model_copy(update={"expected_revision": 1})}
                )
            )
            connection.execute(
                creation_jobs.update()
                .where(creation_jobs.c.id == str(good.id))
                .values(payload=changed.model_dump_json())
            )
    inspection = api.inspect_image_links()
    assert inspection.findings[0].reason != "ready"
    result = api.repair_image_links(inspection.id)
    assert not result.repaired and target.id in result.skipped
    assert api.stories.get(target.id).image_id is None


@pytest.mark.parametrize("change", ["edit", "delete", "file", "job"])
def test_changes_after_inspection_are_rechecked(tmp_path, change):
    api, target, good, _, _, path = retained(tmp_path)
    inspection = api.inspect_image_links()
    if change == "edit":
        api.stories.update(target.id, 0, StoryInput(name="Changed", world="Elsewhere"))
    elif change == "delete":
        api.stories.delete(target.id, 0)
    elif change == "file":
        path.write_bytes(b"changed")
    else:
        with api.library.store.database.engine.begin() as connection:
            connection.execute(
                creation_jobs.update()
                .where(creation_jobs.c.id == str(good.id))
                .values(payload=good.model_copy(update={"error": "changed"}).model_dump_json())
            )
    result = api.repair_image_links(inspection.id)
    assert result.skipped == {target.id: "changed_since_inspection"} and not result.repaired


def test_result_write_failure_resumes_committed_database_without_repairing_twice(
    tmp_path, monkeypatch
):
    api, target, good, _, _, _ = retained(tmp_path)
    inspection = api.inspect_image_links()
    original = repair.atomic_json

    def fail(path, value):
        if path.name == "result.json":
            raise OSError("Injected failure")
        return original(path, value)

    monkeypatch.setattr(repair, "atomic_json", fail)
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.repair_image_links(inspection.id)
    assert api.stories.get(target.id).image_id == good.id
    monkeypatch.setattr(repair, "atomic_json", original)
    assert api.repair_image_links(inspection.id).already_applied == [target.id]


def test_busy_workspace_and_corrupt_backup_are_rejected(tmp_path, monkeypatch):
    api, _, _, _, _, _ = retained(tmp_path)
    inspection = api.inspect_image_links()
    monkeypatch.setattr(repair, "database_in_use", lambda project: True)
    with pytest.raises(KomaquillError) as failure:
        api.repair_image_links(inspection.id)
    assert failure.value.code == "operation_busy"
    monkeypatch.setattr(repair, "database_in_use", lambda project: False)
    folder = tmp_path / ".artifacts/records/image-link-repairs" / str(inspection.id)
    (folder / "before.sqlite3").write_bytes(b"corrupt")
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.repair_image_links(inspection.id)


def test_cli_calls_the_same_public_operations(tmp_path):
    api, target, _, _, _, _ = retained(tmp_path)
    runner = CliRunner()
    response = runner.invoke(app, ["--project", str(tmp_path), "repair-images"])
    assert response.exit_code == 0, response.output
    assert api.stories.get(target.id).image_id is None
    identity = response.output.split()[1].rstrip(":")
    response = runner.invoke(
        app, ["--project", str(tmp_path), "repair-images", "--apply", identity]
    )
    assert response.exit_code == 0, response.output
    assert "1 repaired" in response.output


@pytest.mark.parametrize("kind", ["character", "story"])
def test_retained_full_inputs_are_validated_only_for_explicit_repair(tmp_path, kind):
    api, target, good, failed, _, _ = retained(tmp_path, kind)
    model = CharacterInput if kind == "character" else StoryInput
    original = model.model_validate(
        target.model_dump(include=set(model.model_fields))
    ).model_dump_json()
    with api.library.store.database.engine.begin() as connection:
        for job in (good, failed):
            connection.execute(
                creation_jobs.update()
                .where(creation_jobs.c.id == str(job.id))
                .values(payload=job.model_copy(update={"original": original}).model_dump_json())
            )
    inspection = api.inspect_image_links()
    assert inspection.findings[0].reason == "ready"
    assert api.repair_image_links(inspection.id).repaired == [target.id]
    assert api.repair_image_links(inspection.id).already_applied == [target.id]
    folder = tmp_path / ".artifacts/records/image-link-repairs" / str(inspection.id)
    attempts = [json.loads(p.read_bytes())["result"] for p in (folder / "attempts").glob("*.json")]
    assert any(item["repaired"] for item in attempts)
    assert any(item["already_applied"] for item in attempts)


def test_cli_reports_skipped_candidates_as_incomplete(tmp_path):
    api, _, _, _, _, path = retained(tmp_path)
    path.unlink()
    inspection = api.inspect_image_links()
    response = CliRunner().invoke(
        app, ["--project", str(tmp_path), "repair-images", "--apply", str(inspection.id)]
    )
    assert response.exit_code == 1 and "1 skipped" in response.output
