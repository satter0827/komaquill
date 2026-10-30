"""Exercise Python-facing use cases without CLI wrappers or provider connections."""

import json
from contextlib import contextmanager
from uuid import uuid4

from komaquill.adapters.diagnostics import operation_scope, record_event
from komaquill.api import Client, Evaluation
from komaquill.api import images as services
from komaquill.api import runtime as runtime_services


def test_python_image_and_evaluation_services_use_common_records(
    project, session, client, monkeypatch
):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connect(_):
        yield client, active

    monkeypatch.setattr(services, "connected", connect)
    api = Client(project.root)
    run = api.images.generate("a red flower", seed=9)
    assert run.state == "saved"
    assert api.images.collect(run.run_id).errors == ()
    assert api.evaluations.list() == []
    record = Evaluation(
        run_id=run.run_id, criterion="clarity", reason="One clear subject.", decision="accept"
    )
    assert api.evaluations.add([record]) == [record]
    assert api.evaluations.list(run.run_id)[0].evaluation_id == record.evaluation_id
    assert api.evaluations.export(run.run_id)["runs"][0]["run"]["run_id"] == str(run.run_id)


def test_client_initialization_is_explicit_and_does_not_overwrite(tmp_path):
    root = tmp_path / "without-git"
    api = Client.initialize(root)
    assert api.workspace == root.resolve()
    assert not (root / ".git").exists()
    settings_file = root / "komaquill.toml"
    settings_file.write_text("poll_interval=2.0\n", encoding="utf-8")
    previous = settings_file.read_bytes()
    Client.initialize(root)
    assert settings_file.read_bytes() == previous


def test_operation_ids_do_not_inherit_a_stopped_session(project):
    project.state.mkdir(parents=True)
    identity = uuid4()
    (project.state / "monitor.json").write_text(
        json.dumps({"operation_id": str(uuid4()), "phase": "stopped", "runtime_id": str(identity)})
    )
    with operation_scope() as operation:
        record_event(project, "story_planning", "started")
        record_event(project, "story_planning", "saved")
    events = [json.loads(s) for s in (project.logs / "cli.jsonl").read_text().splitlines()]
    assert {e["operation_id"] for e in events} == {str(operation)}
    assert all(e["session_operation_id"] is None for e in events)


def test_session_service_does_not_authenticate_interactively_by_default(project, monkeypatch):
    seen = []
    monkeypatch.setattr(
        runtime_services.supervisor, "start", lambda *args, **kwargs: seen.append(kwargs)
    )
    api = Client(project.root)
    monkeypatch.setattr(
        runtime_services.inspection,
        "inspect",
        lambda _: __import__("komaquill.domain.status", fromlist=["SessionStatus"]).SessionStatus(
            gpu="active"
        ),
    )
    assert api.runtime.start().gpu == "active"
    assert seen == [{"interactive_auth": False}]


def test_import_is_quiet_and_does_not_load_optional_clients(tmp_path):
    import subprocess
    import sys

    script = (
        "import sys; from komaquill.api import Client; "
        "assert not any(x.startswith(('langchain', 'langsmith', 'colab_cli')) "
        "for x in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_child_operation_id_preserves_an_explicit_operation(project, monkeypatch):
    child = uuid4()
    monkeypatch.setenv("KOMAQUILL_OPERATION_ID", str(child))
    record_event(project, "setup", "started")
    with operation_scope() as current:
        record_event(project, "story_planning", "started")
    monkeypatch.setenv("KOMAQUILL_OPERATION_ID", "invalid-private-value")
    record_event(project, "setup", "failed")
    events = [json.loads(row) for row in (project.logs / "cli.jsonl").read_text().splitlines()]
    assert [row["operation_id"] for row in events] == [str(child), str(current), None]
