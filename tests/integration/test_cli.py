"""Test CLI operations independently of VS Code."""

from contextlib import contextmanager
from types import SimpleNamespace

from typer.testing import CliRunner

from komaquill.infrastructure import inspection, views
from komaquill.interfaces import cli as cli


def test_missing_connection_has_actionable_error(project):
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "collect"])
    assert result.exit_code == 1
    assert "start" in result.output
    assert "Traceback" not in result.output


def test_invalid_view_does_not_open_a_browser():
    result = CliRunner().invoke(cli.app, ["open", "--view", "unknown"])
    assert result.exit_code == 2


def test_open_returns_after_opening_the_selected_view(project, client, session, monkeypatch):
    @contextmanager
    def connection(_project):
        yield client, session

    opened = []
    monkeypatch.setattr(cli, "connected", connection)
    monkeypatch.setattr(
        views.subprocess,
        "run",
        lambda args, **kwargs: opened.append(args[-1]) or SimpleNamespace(returncode=0),
    )
    result = CliRunner().invoke(
        cli.app, ["--project", str(project.root), "open", "--view", "graph"]
    )
    assert result.exit_code == 0, result.output
    assert "template=klein-4b_graph" in opened[0]
    assert "source=komaquill" in opened[0]
    assert "monitor" in result.output


def test_status_without_a_session_never_creates_gpu(project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("確認でGPUを確保してはいけない")

    monkeypatch.setattr(inspection.Colab, "acquire", forbidden)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0, result.output
    assert "Pending: 0" in result.output


def test_collect_reports_partial_save_and_returns_failure(
    project, client, session, stub, monkeypatch
):
    from komaquill.infrastructure.workflows import resource_json

    @contextmanager
    def connection(_project):
        yield client, session

    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "broken-browser")
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "healthy-browser")
    record = next(iter(stub.history.values()))
    record["outputs"]["9"]["images"][0]["filename"] = "broken.png"
    stub.invalid_images.add("broken.png")
    from komaquill.api import services

    monkeypatch.setattr(services, "connected", connection)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "collect"])
    assert result.exit_code == 1
    assert "Saved: 1" in result.output and "collect" in result.output
    assert "Traceback" not in result.output


def test_status_separates_lost_requests_from_pending(project, session):
    from uuid import uuid4

    from komaquill.application.generation import mark_lost, new_run
    from komaquill.infrastructure.atomic import atomic_json
    from komaquill.infrastructure.storage import save_run

    save_run(project, new_run(project, session, uuid4(), {}))
    mark_lost(project, session.runtime_id)
    atomic_json(project.session_file, session.model_dump(mode="json"))
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0
    assert "Pending: 0" in result.output and "Lost: 1" in result.output
    assert "ComfyUI: stopped" in result.output


def test_status_does_not_wait_on_allocation_lock(project, monkeypatch):
    from uuid import uuid4

    from komaquill.infrastructure.atomic import atomic_json
    from komaquill.infrastructure.colab import Colab, Receipt

    colab = Colab(project)
    receipt = Receipt(name="starting", runtime_id=uuid4(), source_revision="a" * 40)
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(inspection, "Colab", lambda project: colab)
    # Use a different lock instance to avoid reentrant locking.
    from filelock import FileLock

    with FileLock(str(project.state / ".colab.lock")):
        result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0
    assert "Colab: starting or stopping" in result.output
    assert "ComfyUI: preparing" in result.output


def test_status_does_not_call_uncertain_allocation_terminated(project, monkeypatch):
    from uuid import uuid4

    from komaquill.infrastructure.atomic import atomic_json
    from komaquill.infrastructure.colab import Colab, Receipt

    colab = Colab(project)
    receipt = Receipt(name="uncertain", runtime_id=uuid4(), source_revision="a" * 40)
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(colab, "refresh", lambda receipt: False)
    monkeypatch.setattr(inspection, "Colab", lambda project: colab)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0
    assert "allocation unconfirmed" in result.output
    assert "ComfyUI: not started" in result.output
    assert "Colab: stopped" not in result.output
    assert colab.receipt_file.is_file()


def test_logs_display_safe_categories_without_exception_payloads(project):
    import json

    folder = project.logs
    folder.mkdir(parents=True)
    row = {
        "created_at": "time",
        "operation": "collection",
        "result": "failed",
        "response_body": "private-response",
        "failure": {"code": "storage_failed", "stage": "storage", "exception_type": "OSError"},
    }
    (folder / "cli.jsonl").write_text(
        json.dumps(row)
        + "\n"
        + json.dumps({"failure": {"code": "unknown", "exception_type": "private-response"}})
        + "\n"
    )
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "logs"])
    assert result.exit_code == 0 and "storage_failed/storage" in result.output
    assert "private-response" not in result.output


def test_status_reports_corrupt_records_without_hiding_other_counts(project):
    folder = project.state / "preparation"
    folder.mkdir(parents=True)
    (folder / "broken.json").write_text("{")
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 1
    assert "unknown" in result.output and "broken.json" in result.output
    assert "Pending: 0" in result.output and "Traceback" not in result.output
