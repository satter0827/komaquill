"""再送・部分保存・画面からの生成を含む回収の検証。"""

import pytest

from komaquill.errors import KomaquillError, SubmissionUnknown
from komaquill.service import collect, generate
from komaquill.storage import load_runs, run_dir
from komaquill.workflow import resource_json


def test_generate_saves_image_and_conditions(project, client, session, stub):
    run = generate(project, client, session, "日本語のプロンプト", "text", 42)
    folder = run_dir(project, run.run_id)
    assert run.state == "saved"
    assert (folder / "images/0001.png").is_file()
    assert (folder / "workflow.json").is_file()
    assert '"seed": 42' in (folder / "workflow-api.json").read_text(encoding="utf-8")
    assert stub.submissions == 1
    assert not (project.root.parent / "outside.png").exists()


def test_repeat_collection_does_not_download_again(project, client, session, stub):
    generate(project, client, session, "a cabin", "", 1)
    assert collect(project, client, session).saved == 0
    assert stub.image_requests == 1


def test_lost_submission_response_is_reconciled_without_resubmission(
    project, client, session, stub
):
    stub.drop_response = True
    with pytest.raises(SubmissionUnknown):
        generate(project, client, session, "a cabin", "", 1)
    assert load_runs(project)[0].state == "unknown"
    assert collect(project, client, session).saved == 1
    assert load_runs(project)[0].state == "saved"
    assert stub.submissions == 1


@pytest.mark.parametrize("failure", ["image_fails", "corrupt_image"])
def test_partial_download_can_be_resumed(project, client, session, stub, failure):
    setattr(stub, failure, True)
    with pytest.raises(KomaquillError):
        generate(project, client, session, "a cabin", "", 1)
    assert load_runs(project)[0].state == "queued"
    assert not list(project.runs.glob("*/images/*"))
    setattr(stub, failure, False)
    assert collect(project, client, session).saved == 1
    assert stub.submissions == 1


def test_ui_generation_is_imported_and_collected(project, client, session, stub):
    client.submit(resource_json("workflow-api.json"), resource_json("workflow.json"), "ui-client")
    assert collect(project, client, session).saved == 1
    run = load_runs(project)[0]
    assert run.state == "saved"
    assert collect(project, client, session).saved == 0


def test_remote_failure_remains_failed(project, client, session, stub):
    client.submit(resource_json("workflow-api.json"), {}, "ui-client")
    record = next(iter(stub.history.values()))
    record["status"] = {"status_str": "error", "completed": False}
    assert collect(project, client, session).failed == 1
    assert load_runs(project)[0].state == "failed"
    assert stub.image_requests == 0


def test_runtime_replacement_does_not_mix_histories(project, client, session):
    session.runtime_id = __import__("uuid").uuid4()
    with pytest.raises(KomaquillError, match="ランタイムが変わりました"):
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
    run = generate(project, client, session, "a cabin", "", 1)
    assert run.state == "saved"
    assert stub_images(project) == 1


def stub_images(project):
    return len(list(project.runs.glob("*/images/*.png")))


def test_external_does_not_require_metadata_and_excludes_old_history(project, client, session):
    session.backend = "external"
    old_id = client.submit(resource_json("workflow-api.json"), {}, "old-browser")
    session.excluded_prompt_ids = {old_id}
    assert collect(project, client, session).saved == 0
    client.submit(resource_json("workflow-api.json"), {}, "new-browser")
    assert collect(project, client, session).saved == 1
    assert len(load_runs(project)) == 1


def test_external_own_pending_request_is_recovered_even_if_in_baseline(
    project, client, session, stub
):
    session.backend = "external"
    stub.drop_response = True
    with pytest.raises(SubmissionUnknown):
        generate(project, client, session, "a cabin", "", 1)
    session.excluded_prompt_ids = set(stub.history)
    assert collect(project, client, session).saved == 1
    assert load_runs(project)[0].state == "saved"
