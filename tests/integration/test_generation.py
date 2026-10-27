"""Test reconciliation, partial persistence and browser generation."""

import pytest

from komaquill.application.generation import collect, generate
from komaquill.domain.errors import KomaquillError, SubmissionUnknown
from komaquill.infrastructure.storage import load_runs, run_dir
from komaquill.infrastructure.workflows import resource_json


def test_generate_saves_image_and_conditions(project, client, session, stub):
    run = generate(project, client, session, "日本語のプロンプト", 42)
    folder = run_dir(project, run.run_id)
    assert run.state == "saved"
    assert (folder / "images/0001.png").is_file()
    assert (folder / "workflow.json").is_file()
    assert '"noise_seed": 42' in (folder / "workflow-api.json").read_text(encoding="utf-8")
    assert stub.submissions == 1
    assert not (project.root.parent / "outside.png").exists()


def test_repeat_collection_does_not_download_again(project, client, session, stub):
    generate(project, client, session, "a cabin", 1)
    assert collect(project, client, session).saved == 0
    assert stub.image_requests == 1


def test_lost_submission_response_is_reconciled_without_resubmission(
    project, client, session, stub
):
    stub.drop_response = True
    with pytest.raises(SubmissionUnknown):
        generate(project, client, session, "a cabin", 1)
    assert load_runs(project)[0].state == "unknown"
    assert collect(project, client, session).saved == 1
    assert load_runs(project)[0].state == "saved"
    assert stub.submissions == 1


@pytest.mark.parametrize("failure", ["image_fails", "corrupt_image"])
def test_partial_download_can_be_resumed(project, client, session, stub, failure):
    setattr(stub, failure, True)
    with pytest.raises(KomaquillError):
        generate(project, client, session, "a cabin", 1)
    assert load_runs(project)[0].state == "queued"
    assert not list(project.runs.glob("*/images/*"))
    setattr(stub, failure, False)
    assert collect(project, client, session).saved == 1
    assert stub.submissions == 1


def test_ui_generation_is_imported_and_collected(project, client, session, stub):
    client.submit(
        resource_json("presets/klein-4b/workflow-api.json"),
        resource_json("presets/klein-4b/workflow.json"),
        "ui-client",
    )
    assert collect(project, client, session).saved == 1
    run = load_runs(project)[0]
    assert run.state == "saved"
    assert collect(project, client, session).saved == 0


def test_remote_failure_remains_failed(project, client, session, stub):
    graph = resource_json("presets/klein-4b/workflow-api.json")
    graph["5"]["inputs"]["batch_size"] = 2
    client.submit(graph, {}, "ui-client")
    record = next(iter(stub.history.values()))
    record["status"] = {"status_str": "error", "completed": False}
    assert collect(project, client, session).failed == 1
    assert load_runs(project)[0].state == "failed"
    run = load_runs(project)[0]
    assert len(run.images) == 2 and run.saved_at is None
    fetched = stub.image_requests
    assert collect(project, client, session).saved == 0
    assert stub.image_requests == fetched


def test_runtime_replacement_does_not_mix_histories(project, client, session):
    session.runtime_id = __import__("uuid").uuid4()
    with pytest.raises(KomaquillError):
        collect(project, client, session)


def test_collector_finishing_before_post_response_preserves_saved_state(
    project, client, session, monkeypatch
):
    original = client.submit

    def submit(graph, ui, run_id):
        prompt_id = original(graph, ui, run_id)
        assert collect(project, client, session).saved == 1
        return prompt_id

    monkeypatch.setattr(client, "submit", submit)
    run = generate(project, client, session, "a cabin", 1)
    assert run.state == "saved"
    assert stub_images(project) == 1


def stub_images(project):
    return len(list(project.runs.glob("*/images/*.png")))


def test_external_does_not_require_metadata_and_excludes_old_history(project, client, session):
    session.backend = "external"
    old_id = client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "old-browser")
    session.excluded_prompt_ids = {old_id}
    assert collect(project, client, session).saved == 0
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "new-browser")
    assert collect(project, client, session).saved == 1
    assert len(load_runs(project)) == 1


def test_external_own_pending_request_is_recovered_even_if_in_baseline(
    project, client, session, stub
):
    session.backend = "external"
    stub.drop_response = True
    with pytest.raises(SubmissionUnknown):
        generate(project, client, session, "a cabin", 1)
    session.excluded_prompt_ids = set(stub.history)
    assert collect(project, client, session).saved == 1
    assert load_runs(project)[0].state == "saved"


def test_external_recovers_own_pending_request_after_connection_identity_changes(
    project, client, session, stub
):
    from uuid import uuid4

    stub.drop_response = True
    with pytest.raises(SubmissionUnknown):
        generate(project, client, session, "a cabin", 1)
    original = load_runs(project)[0]
    session = session.model_copy(
        update={
            "backend": "external",
            "runtime_id": uuid4(),
            "excluded_prompt_ids": set(stub.history),
        }
    )
    result = collect(project, client, session)
    recovered = load_runs(project)[0]
    assert result.saved == 1
    assert result.pending == 0
    assert recovered.run_id == original.run_id
    assert recovered.runtime_id == original.runtime_id
    assert recovered.state == "saved"
    assert stub.submissions == 1


def test_one_corrupt_result_does_not_block_healthy_results(project, client, session, stub):
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "broken-browser")
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "healthy-browser")
    first = next(iter(stub.history.values()))
    first["outputs"]["9"]["images"][0]["filename"] = "broken.png"
    stub.invalid_images.add("broken.png")
    result = collect(project, client, session)
    assert result.saved == 1 and len(result.errors) == 1 and result.pending == 1
    assert sorted(run.state for run in load_runs(project)) == ["queued", "saved"]
    stub.invalid_images.clear()
    assert collect(project, client, session).saved == 1
    assert collect(project, client, session).saved == 0


def test_browser_queue_is_recorded_before_completion(project, client, session, stub):
    from uuid import uuid4

    prompt_id = str(uuid4())
    prompt = [
        0,
        prompt_id,
        resource_json("presets/klein-4b/workflow-api.json"),
        {"extra_pnginfo": {"workflow": resource_json("presets/klein-4b/workflow.json")}},
        ["9"],
    ]
    stub.queue["queue_running"] = [prompt]
    result = collect(project, client, session)
    run = load_runs(project)[0]
    assert result.pending_ids == (run.run_id,)
    assert run.prompt_id == prompt_id and run.state == "queued"
    assert (run_dir(project, run.run_id) / "workflow-api.json").is_file()
    assert (run_dir(project, run.run_id) / "workflow.json").is_file()


def test_lost_runs_never_return_to_pending(project, client, session, stub):
    from uuid import uuid4

    from komaquill.application.generation import mark_lost, new_run
    from komaquill.infrastructure.storage import save_run

    run = new_run(project, session, uuid4(), {})
    save_run(project, run)
    assert mark_lost(project, session.runtime_id) == (run.run_id,)
    assert not load_runs(project)[0].pending
    assert collect(project, client, session).pending == 0


def test_shutdown_request_prevents_new_submission(project, client, session, stub):
    from komaquill.infrastructure.atomic import atomic_json

    atomic_json(project.state / "stop.json", {"runtime_id": str(session.runtime_id), "force": True})
    with pytest.raises(KomaquillError):
        generate(project, client, session, "a cabin", 1)
    assert stub.submissions == 0
    assert load_runs(project) == []


def test_lost_state_is_preserved_when_submission_response_arrives_late(
    project, client, session, monkeypatch
):
    from komaquill.application.generation import mark_lost

    original = client.submit

    def submit(graph, ui, run_id):
        result = original(graph, ui, run_id)
        mark_lost(project, session.runtime_id)
        return result

    monkeypatch.setattr(client, "submit", submit)
    with pytest.raises(KomaquillError):
        generate(project, client, session, "a cabin", 1)
    assert load_runs(project)[0].state == "lost"


def test_local_save_failure_does_not_block_other_runs_or_store_exception_text(
    project, client, session, monkeypatch
):

    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "first-browser")
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "second-browser")
    from komaquill.infrastructure import atomic as storage

    original = storage.atomic_bytes
    calls = []

    def save(path, content):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("private-exception-message")
        original(path, content)

    monkeypatch.setattr(storage, "atomic_bytes", save)
    result = collect(project, client, session)
    assert result.saved == 1 and len(result.errors) == 1
    assert all("private-exception-message" not in (run.error or "") for run in load_runs(project))


@pytest.mark.parametrize("record", [None, {"prompt": None}, {"prompt": {"unexpected": True}}])
def test_malformed_history_entry_does_not_block_other_jobs(project, client, session, stub, record):
    from uuid import uuid4

    stub.history[str(uuid4())] = record
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "healthy-browser")
    result = collect(project, client, session)
    assert result.saved == 1
    assert len(result.errors) == 1
    assert result.pending == 1
    assert sorted(run.state for run in load_runs(project)) == ["queued", "saved"]


def test_unreadable_local_run_does_not_block_other_jobs(project, client, session, stub):
    from uuid import uuid4

    broken_id = uuid4()
    folder = run_dir(project, broken_id)
    folder.mkdir(parents=True)
    damaged = b"invalid-json-private-sentinel"
    (folder / "run.json").write_bytes(damaged)
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "healthy-browser")
    result = collect(project, client, session)
    assert result.saved == 1
    assert result.errors == (broken_id,)
    assert (folder / "run.json").read_bytes() == damaged


def test_malformed_known_job_reports_its_original_run_id(project, client, session, stub):
    from uuid import uuid4

    from komaquill.application.generation import new_run
    from komaquill.infrastructure.storage import save_run

    prompt_id = client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "browser")
    run = new_run(project, session, uuid4(), resource_json("presets/klein-4b/workflow-api.json"))
    run.prompt_id = prompt_id
    run.state = "queued"
    save_run(project, run)
    stub.history[prompt_id] = None
    result = collect(project, client, session, only=run.run_id)
    assert result.errors == (run.run_id,)


def test_retry_after_malformed_history_preserves_workflow_digest(project, client, session, stub):
    from komaquill.domain.workflow import digest

    prompt_id = client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "browser")
    complete = stub.history[prompt_id]
    stub.history[prompt_id] = None
    assert len(collect(project, client, session).errors) == 1
    stub.history[prompt_id] = complete
    assert collect(project, client, session).saved == 1
    assert load_runs(project)[0].workflow_sha256 == digest(complete["prompt"][2])


def test_corrupt_record_is_not_overwritten_by_remote_history(project, client, session, stub):
    from uuid import uuid4

    identity = uuid4()
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, str(identity))
    folder = run_dir(project, identity)
    folder.mkdir(parents=True)
    original = b"original-damaged-record"
    (folder / "run.json").write_bytes(original)
    result = collect(project, client, session)
    assert result.errors == (identity,)
    assert (folder / "run.json").read_bytes() == original
    assert not list(folder.glob("images/*"))
