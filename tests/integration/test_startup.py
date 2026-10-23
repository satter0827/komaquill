"""Test the complete startup lifecycle through mocked provider boundaries."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import ShutdownResult
from komaquill.infrastructure import runtime as runtime
from komaquill.infrastructure import views
from komaquill.infrastructure.colab import Colab, Receipt
from komaquill.infrastructure.storage import atomic_json, load_session
from komaquill.infrastructure.workflows import preset, resource_json
from komaquill.interfaces import cli as cli
from tests.support.provider import stopped as confirmed_stop


@pytest.mark.parametrize(
    "case",
    ["new", "existing", "retry", "startup_failure", "allocation_failure", "diagnostics_failure"],
)
def test_colab_startup_freezes_configuration_and_always_finalizes_owned_runtime(
    project, session, client, monkeypatch, case
):
    control = Colab(project)
    receipt = Receipt(
        name="owned",
        runtime_id=session.runtime_id,
        source_revision="revision",
        endpoint_id="expected",
        settings=project.settings(),
    )
    calls = []
    if case == "existing":
        atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(runtime, "Colab", lambda _: control)

    monkeypatch.setattr(runtime, "fetch_model", lambda *args: None)
    monkeypatch.setattr(runtime, "check_port", lambda _: None)
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)

    def acquire(revision):
        calls.append("acquire")
        atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
        if case == "allocation_failure":
            raise KomaquillError("allocation failed")
        return receipt, case != "existing"

    monkeypatch.setattr(control, "acquire", acquire)
    process = SimpleNamespace(poll=lambda: 1 if case == "startup_failure" else None)
    monkeypatch.setattr(control, "prepare", lambda *args: calls.append("prepare") or process)
    monkeypatch.setattr(control, "refresh", lambda _: True)
    monkeypatch.setattr(control, "tunnel", lambda _: calls.append("ssh") or process)
    monkeypatch.setattr(control, "recover_diagnostics", lambda _: calls.append("logs") or ())
    monkeypatch.setattr(control, "stop", lambda: calls.append("stop") or confirmed_stop())
    monkeypatch.setattr(control, "cleanup_processes", lambda _: calls.append("cleanup"))
    monkeypatch.setattr(runtime, "terminate", lambda *args: calls.append("close"))
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    original = client.runtime
    reads = []

    def metadata():
        reads.append(True)
        if case == "startup_failure" or (case == "retry" and len(reads) == 1):
            raise KomaquillError("server not ready")
        return original()

    monkeypatch.setattr(client, "runtime", metadata)

    def watch(bound, connection, current, echo, reconnect):
        assert current.settings == receipt.settings
        (project.root / "komaquill.toml").write_text("invalid [")
        assert bound.settings().local_port == 18188
        reconnect()
        return True

    monkeypatch.setattr(runtime, "watch", watch)
    if case == "diagnostics_failure":
        monkeypatch.setattr(runtime, "record_event", lambda *args, **kwargs: False)
    if case in {"startup_failure", "allocation_failure", "diagnostics_failure"}:
        with pytest.raises(KomaquillError):
            runtime.start_colab(project, lambda _: None)
    else:
        runtime.start_colab(project, lambda _: None)
        assert load_session(project).settings == receipt.settings
        summary = ShutdownResult.model_validate_json(
            (project.artifacts / "logs" / str(receipt.runtime_id) / "shutdown.json").read_bytes()
        )
        assert summary.gpu == "stopped" and summary.history == "ok"
    assert not control.receipt_file.exists()
    assert ("prepare" in calls) == (
        case not in {"existing", "allocation_failure", "diagnostics_failure"}
    )
    assert ("stop" in calls) == (case != "diagnostics_failure")


def test_credentials_are_loaded_only_for_the_saved_connection(
    project, session, client, monkeypatch
):
    session.username = "user"
    session.settings = project.settings()
    atomic_json(project.session_file, session.model_dump(mode="json"))
    monkeypatch.setattr(runtime.keyring, "get_password", lambda *args: None)
    with pytest.raises(KomaquillError), runtime.connected(project):
        pass
    seen = []
    monkeypatch.setattr(runtime.keyring, "get_password", lambda *args: "private-password")
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: seen.append(args) or client)
    with runtime.connected(project) as (_, saved):
        assert saved.username == "user"
    assert seen[0][1] == "private-password"
    assert "private-password" not in project.session_file.read_text()


@pytest.fixture
def cli_connection(project, session, client, monkeypatch):
    @contextmanager
    def connected(_):
        yield client, session

    monkeypatch.setattr(cli, "connected", connected)
    return lambda *args: CliRunner().invoke(cli.app, ["--project", str(project.root), *args])


@pytest.mark.parametrize("case", ["ready", "nodes", "model", "cpu"])
def test_doctor_reports_actual_generation_requirements(cli_connection, client, monkeypatch, case):
    original = client.json
    nodes = {row["class_type"]: {} for row in resource_json("workflow-api.json").values()}
    nodes["CheckpointLoaderSimple"] = {"input": {"required": {"ckpt_name": [[preset().filename]]}}}
    if case == "nodes":
        nodes.pop("KSampler")
    if case == "model":
        nodes["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [[]]

    def response(path):
        if path == "/object_info":
            return nodes
        if path == "/system_stats":
            return {"devices": [{"type": "cpu" if case == "cpu" else "cuda"}]}
        return original(path)

    monkeypatch.setattr(client, "json", response)
    result = cli_connection("doctor")
    assert result.exit_code == (0 if case == "ready" else 1), result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("prompt", ["a cabin", ""])
def test_generate_cli_prompts_and_returns_saved_path(cli_connection, monkeypatch, prompt):
    monkeypatch.setattr(cli.typer, "prompt", lambda _: prompt)
    result = cli_connection("generate")
    assert result.exit_code == (0 if prompt else 1), result.output
    if prompt:
        assert "Saved:" in result.output


@pytest.mark.parametrize("command", ["start", "stop", "models"])
def test_cli_delegates_operations_without_editor_dependencies(project, monkeypatch, command):
    calls = []
    monkeypatch.setattr(cli.runtime, "start", lambda *args: calls.append(args))
    monkeypatch.setattr(cli.runtime, "stop", lambda *args: calls.append(args))
    monkeypatch.setattr(
        cli, "fetch_model", lambda *args: calls.append(args) or project.artifacts / "model"
    )
    monkeypatch.setattr(cli.typer, "prompt", lambda *args, **kwargs: "private-password")
    args = {
        "start": [
            "start",
            "--backend",
            "external",
            "--endpoint",
            "http://127.0.0.1:8188",
            "--username",
            "user",
        ],
        "stop": ["stop", "--force"],
        "models": ["models", "fetch"],
    }[command]
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), *args])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and "private-password" not in result.output


def test_external_open_exports_workflow_and_returns_to_terminal(
    cli_connection, session, project, monkeypatch
):
    session.backend = "external"
    monkeypatch.setattr(
        views.webbrowser,
        "open",
        lambda _: True,
    )
    result = cli_connection("open", "--view", "app")
    assert result.exit_code == 0, result.output
    assert (project.runtime / "workflows/pony_app.json").is_file()
