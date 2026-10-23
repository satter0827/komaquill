"""Exercise recovery records, settings lifetime, and concurrent persistence."""

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from komaquill import cli, runtime
from komaquill.colab import Colab, Receipt
from komaquill.errors import KomaquillError, RuntimeEnded
from komaquill.events import failure_info, record_event
from komaquill.models import ShutdownResult
from komaquill.service import collect, generate, generation_times, new_run
from komaquill.storage import atomic_json, load_runs, load_session, run_dir
from komaquill.workflow import resource_json


def test_saved_images_record_integrity_and_server_times(project, client, session, stub):
    prompt_id = client.submit(resource_json("workflow-api.json"), {}, "browser")
    stub.history[prompt_id]["status"]["messages"] = [
        ["execution_start", {"timestamp": 1000}],
        ["execution_success", {"timestamp": 2500}],
    ]
    assert collect(project, client, session).saved == 1
    run = load_runs(project)[0]
    image = run_dir(project, run.run_id) / run.images[0]
    metadata = run.image_metadata[run.images[0]]
    assert metadata.sha256 == hashlib.sha256(image.read_bytes()).hexdigest()
    assert metadata.size_bytes == image.stat().st_size
    assert (metadata.width, metadata.height) == (8, 8)
    assert run.generation_started_at == datetime.fromtimestamp(1, UTC)
    assert run.generation_completed_at == datetime.fromtimestamp(2.5, UTC)
    assert run.saved_at is not None and run.updated_at >= run.saved_at
    before = (run_dir(project, run.run_id) / "run.json").read_bytes()
    assert collect(project, client, session).saved == 0
    assert (run_dir(project, run.run_id) / "run.json").read_bytes() == before


def test_partial_images_are_reused_and_tampered_images_are_repaired(project, client, session, stub):
    prompt = client.submit(resource_json("workflow-api.json"), {}, "browser")
    record = stub.history[prompt]
    record["outputs"]["9"]["images"].append({"filename": "broken.png"})
    stub.invalid_images.add("broken.png")
    first = collect(project, client, session)
    run = load_runs(project)[0]
    assert first.pending == 1 and run.failure.code == "image_invalid"
    assert run.failure.stage == "image_validation"
    target = run_dir(project, run.run_id) / run.images[0]
    target.write_bytes(b"changed after partial recovery")
    stub.invalid_images.clear()
    assert collect(project, client, session).saved == 1
    saved = load_runs(project)[0]
    assert len(saved.images) == 2 and saved.failure is None
    assert (
        hashlib.sha256(target.read_bytes()).hexdigest()
        == saved.image_metadata[saved.images[0]].sha256
    )


def test_parallel_collectors_commit_each_image_once(project, client, session, stub):
    client.submit(resource_json("workflow-api.json"), {}, "browser")
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: collect(project, client, session), range(3)))
    assert sum(result.saved for result in results) == 1
    assert stub.image_requests == 1
    assert len(load_runs(project)) == 1
    assert load_runs(project)[0].state == "saved"


@pytest.mark.parametrize(
    "messages",
    [
        None,
        [
            None,
            [],
            ["ignored", {}],
            ["execution_start", {"timestamp": True}],
            ["execution_start", {"timestamp": "1000"}],
            ["execution_start", {"timestamp": float("inf")}],
        ],
    ],
)
def test_unknown_server_times_are_not_invented(project, session, messages):
    run = new_run(project, session, uuid4(), {})
    generation_times(run, {"messages": messages})
    assert run.generation_started_at is None and run.generation_completed_at is None


def test_event_log_appends_safe_failures_and_survives_parallel_processes(project):
    code = (
        "from pathlib import Path; from komaquill.events import record_event; "
        "from komaquill.settings import Project; import sys; "
        "p=Project(Path(sys.argv[1])); "
        "[record_event(p,'check','ok') for _ in range(5)]"
    )
    children = [
        subprocess.Popen([sys.executable, "-B", "-c", code, str(project.root)]) for _ in range(2)
    ]
    for child in children:
        assert child.wait(timeout=20) == 0
    failure = failure_info(RuntimeError("private-value-must-not-appear"), "connection")
    assert record_event(project, "connect", "failed", failure=failure)
    text = (project.artifacts / "logs/cli.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]
    assert len(events) == 11 and "private-value-must-not-appear" not in text
    assert events[-1]["failure"]["exception_type"] == "RuntimeError"


def test_log_write_failure_reports_without_raising_private_error(project, monkeypatch, capsys):
    original = Path.open

    def denied(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            raise PermissionError("private-value")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert not record_event(project, "connect", "failed")
    assert "操作ログ" in capsys.readouterr().err


def test_external_start_freezes_settings_and_keeps_unfinished_work(project, client, monkeypatch):
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)
    observed = []

    def watch(bound, connection, session, echo):
        observed.append(session.settings.poll_interval)
        (project.root / "komaquill.toml").write_text("not valid toml [")
        run = generate(bound, connection, session, "a cabin", "", 1)
        assert run.state == "saved"
        assert bound.settings() is session.settings
        return True

    monkeypatch.setattr(runtime, "watch", watch)
    runtime.start_external(project, "http://127.0.0.1:8188", None, None, lambda _: None)
    session = load_session(project)
    assert observed == [0.001] and session.settings.poll_interval == 0.001
    summary = ShutdownResult.model_validate_json(
        (project.artifacts / "logs" / str(session.runtime_id) / "shutdown.json").read_bytes()
    )
    assert summary.gpu == "not_managed" and summary.history == "ok"


def test_configuration_refreshes_only_on_the_next_start(project, client, monkeypatch):
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(runtime, "watch", lambda *args: True)
    runtime.start_external(project, "http://127.0.0.1:8188", None, None, lambda _: None)
    before = load_session(project)
    (project.root / "komaquill.toml").write_text("poll_interval = 0.25")
    assert before.settings.poll_interval == 0.001
    runtime.start_external(project, "http://127.0.0.1:8188", None, None, lambda _: None)
    after = load_session(project)
    assert after.runtime_id == before.runtime_id and after.settings.poll_interval == 0.25


def test_owned_settings_survive_invalid_project_configuration(project):
    colab = Colab(project)
    receipt = Receipt(
        name="owned", runtime_id=uuid4(), source_revision="a" * 40, settings=project.settings()
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    (project.root / "komaquill.toml").write_text("invalid [")
    assert Colab(project).settings.poll_interval == 0.001


def test_watch_stops_retrying_a_confirmed_ended_runtime(project, client, session):
    def ended():
        raise RuntimeEnded("ended")

    assert runtime.watch(project, client, session, lambda _: None, ended) is True
    text = (project.artifacts / "logs" / str(session.runtime_id) / "events.jsonl").read_text()
    assert "runtime_ended" in text


def test_browser_failure_returns_url_and_failure_exit(project, client, session, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def connection(_project):
        yield client, session

    monkeypatch.setattr(cli, "connected", connection)
    monkeypatch.setattr(cli.webbrowser, "open", lambda _: False)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "open"])
    assert result.exit_code == 1 and "http://127.0.0.1" in result.output
    assert "起動しました" not in result.output


def test_capture_records_freshness_without_destroying_last_success(project, monkeypatch):
    colab = Colab(project)
    receipt = Receipt(name="owned", runtime_id=uuid4(), source_revision="a" * 40)
    monkeypatch.setattr(colab, "entry", lambda _: {})
    monkeypatch.setattr(
        colab, "call", lambda *args, **kwargs: Path(args[-1]).write_bytes(b"complete")
    )
    assert not colab.recover_diagnostics(receipt)
    folder = project.artifacts / "logs" / str(receipt.runtime_id)
    before = json.loads((folder / "capture.json").read_bytes())

    def fail(*args, **kwargs):
        raise KomaquillError("private-response")

    monkeypatch.setattr(colab, "call", fail)
    assert len(colab.recover_diagnostics(receipt)) == 3
    after = json.loads((folder / "capture.json").read_bytes())
    for name, item in after["files"].items():
        assert item["last_success_at"] == before["files"][name]["last_success_at"]
        assert item["result"] == "failed" and (folder / name).read_bytes() == b"complete"
    assert "private-response" not in json.dumps(after)


def test_status_uses_saved_settings_even_if_configuration_is_invalid(project, session, monkeypatch):
    session.backend = "external"
    session.settings = project.settings()
    atomic_json(project.session_file, session.model_dump(mode="json"))
    (project.root / "komaquill.toml").write_text("invalid [")
    monkeypatch.setattr(
        cli, "connected", lambda _: (_ for _ in ()).throw(KomaquillError("disconnected"))
    )
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0, result.output
    assert "接続できません" in result.output


def test_timeouts_must_be_finite():
    from pydantic import ValidationError

    from komaquill.settings import Settings

    with pytest.raises(ValidationError):
        Settings(request_timeout=float("inf"))


def test_unknown_submission_records_safe_failure_before_reconciliation(
    project, client, session, stub
):
    from komaquill.errors import SubmissionUnknown
    from komaquill.storage import load_runs

    stub.drop_response = True
    with pytest.raises(SubmissionUnknown):
        generate(project, client, session, "a cabin", "", 2)
    run = load_runs(project)[0]
    assert run.state == "unknown" and run.failure.code == "submission_unknown"
    assert run.failure.stage == "submission" and run.failure.retryable
    assert collect(project, client, session).saved == 1
    assert load_runs(project)[0].failure is None


def test_log_temporary_cleanup_failure_does_not_skip_other_files(project, monkeypatch):
    colab = Colab(project)
    receipt = Receipt(name="owned", runtime_id=uuid4())
    monkeypatch.setattr(colab, "entry", lambda _: {})
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.name.startswith(".bootstrap.log."):
            raise PermissionError("private filesystem information")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(colab, "call", lambda *args, **kwargs: Path(args[-1]).write_bytes(b"log"))
    assert colab.recover_diagnostics(receipt) == ("bootstrap.log",)
    folder = project.artifacts / "logs" / str(receipt.runtime_id)
    report = json.loads((folder / "capture.json").read_bytes())
    assert report["files"]["bootstrap.log"]["failure"]["stage"] == "log_cleanup"
    assert report["files"]["comfy.log"]["result"] == "saved"
    assert "private filesystem information" not in json.dumps(report)


def test_forced_shutdown_reports_lost_count_without_claiming_full_collection(
    project, client, session, monkeypatch
):
    from komaquill.service import new_run
    from komaquill.storage import save_run

    run = new_run(project, session, uuid4(), {})
    save_run(project, run)
    colab = Colab(project)
    receipt = Receipt(name="owned", runtime_id=session.runtime_id, settings=project.settings())
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(colab, "recover_diagnostics", lambda _: ())
    monkeypatch.setattr(colab, "stop", lambda: None)
    monkeypatch.setattr(colab, "cleanup_processes", lambda _: None)
    messages = []
    runtime.finish_colab(project, colab, receipt, client, session, True, messages.append, [])
    result = ShutdownResult.model_validate_json(
        (project.artifacts / "logs" / str(session.runtime_id) / "shutdown.json").read_bytes()
    )
    assert result.collection == "incomplete" and result.lost_ids == [run.run_id]
    assert not result.errors and not colab.receipt_file.exists()
    assert any("回収不能: 1件" in message for message in messages)
    assert any("画像回収: 未回収あり" in message for message in messages)
