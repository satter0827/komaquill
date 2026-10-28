"""Test the complete startup lifecycle through mocked provider boundaries."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from komaquill.api import services
from komaquill.domain.errors import FailureInfo, KomaquillError
from komaquill.domain.models import ShutdownResult
from komaquill.infrastructure import views
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab, Receipt
from komaquill.infrastructure.sessions import connection, startup
from komaquill.infrastructure.storage import load_session
from komaquill.interfaces import cli as cli
from tests.support.provider import stopped as confirmed_stop
from tests.support.session_controls import patch_session


@pytest.mark.parametrize(
    "case",
    [
        "new",
        "existing",
        "resume_preparation",
        "retry",
        "startup_failure",
        "allocation_failure",
        "diagnostics_failure",
        "simultaneous_failure",
    ],
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
    if case in {"existing", "resume_preparation"}:
        atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    if case == "existing":
        atomic_json(project.session_file, session.model_dump(mode="json"))
    patch_session(monkeypatch, "Colab", lambda _: control)

    patch_session(monkeypatch, "preflight", lambda *args: None)
    patch_session(monkeypatch, "check_port", lambda _: None)
    patch_session(monkeypatch, "ComfyClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)
    patch_session(monkeypatch, "verify_extension", lambda _: None)

    def acquire(revision):
        calls.append("acquire")
        atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
        if case == "allocation_failure":
            raise KomaquillError("allocation failed")
        return receipt, case not in {"existing", "resume_preparation"}

    monkeypatch.setattr(control, "acquire", acquire)
    process = SimpleNamespace(poll=lambda: 1 if case == "startup_failure" else None)
    monkeypatch.setattr(control, "prepare", lambda *args: calls.append("prepare") or process)
    monkeypatch.setattr(
        control,
        "progress",
        lambda _: (
            {
                "phase": "failed",
                "failure": FailureInfo(
                    code="connection_failed",
                    stage="preparation",
                    exception_type="ConnectError",
                    http_status=503,
                    retryable=True,
                ),
            }
            if case == "startup_failure"
            else {}
        ),
    )
    monkeypatch.setattr(control, "refresh", lambda _: True)
    monkeypatch.setattr(control, "tunnel", lambda _: calls.append("ssh") or process)
    monkeypatch.setattr(control, "recover_diagnostics", lambda _: calls.append("logs") or ())
    monkeypatch.setattr(control, "stop", lambda: calls.append("stop") or confirmed_stop())
    monkeypatch.setattr(control, "cleanup_processes", lambda _: calls.append("cleanup"))
    patch_session(monkeypatch, "terminate", lambda *args: calls.append("close"))
    monkeypatch.setattr(startup.time, "sleep", lambda _: None)
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

    patch_session(monkeypatch, "watch", watch)
    if case == "simultaneous_failure":

        def fail_progress(_):
            raise PermissionError("private-path")

        patch_session(monkeypatch, "PROGRESS_POLL_SECONDS", 0)
        monkeypatch.setattr(control, "progress", fail_progress)
        monkeypatch.setattr(control, "recover_diagnostics", lambda _: ("comfy.log",))
    elif case == "diagnostics_failure":
        patch_session(monkeypatch, "record_event", lambda *args, **kwargs: False)
    if case == "simultaneous_failure":
        with pytest.raises(PermissionError):
            startup.start_colab(project, lambda _: None)
    elif case in {"startup_failure", "allocation_failure", "diagnostics_failure"}:
        with pytest.raises(KomaquillError) as caught:
            startup.start_colab(project, lambda _: None)
        if case == "startup_failure":
            assert caught.value.http_status == 503 and caught.value.retryable
    else:
        startup.start_colab(project, lambda _: None)
        assert load_session(project).settings == receipt.settings
        summary = ShutdownResult.model_validate_json(
            (project.logs / str(receipt.runtime_id) / "shutdown.json").read_bytes()
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
    monkeypatch.setattr(connection.keyring, "get_password", lambda *args: None)
    with pytest.raises(KomaquillError), connection.connected(project):
        pass
    seen = []
    monkeypatch.setattr(connection.keyring, "get_password", lambda *args: "private-password")
    patch_session(monkeypatch, "ComfyClient", lambda *args, **kwargs: seen.append(args) or client)
    with connection.connected(project) as (_, saved):
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
    from komaquill.domain.presets import MODEL_INPUT_ROLES
    from komaquill.infrastructure.configuration import Settings

    settings = Settings()
    nodes = {
        node["class_type"]: {}
        for profile in settings.presets.values()
        for node in profile.api.values()
    }
    for kind, (field, role) in MODEL_INPUT_ROLES.items():
        choices = [asset.reference for asset in settings.assets.values() if asset.role == role]
        nodes[kind] = {"input": {"required": {field: [choices]}}}
    if case == "nodes":
        nodes.pop("SamplerCustomAdvanced")
    if case == "model":
        nodes["UNETLoader"]["input"]["required"]["unet_name"] = [[]]

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
    monkeypatch.setattr(services, "connected", cli.connected)
    monkeypatch.setattr(cli.typer, "prompt", lambda _: prompt)
    result = cli_connection("generate")
    assert result.exit_code == (0 if prompt else 1), result.output
    if prompt:
        assert "Saved:" in result.output


@pytest.mark.parametrize("command", ["start", "stop"])
def test_cli_delegates_operations_without_editor_dependencies(project, monkeypatch, command):
    calls = []
    monkeypatch.setattr(services.supervisor, "start", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(services.shutdown, "stop", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(cli.typer, "prompt", lambda *args, **kwargs: "private-password")
    monkeypatch.setattr(cli.keyring, "set_password", lambda *args: None)
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
    assert len(calls) == (5 if command == "models" else 1)
    assert "private-password" not in result.output


def test_external_open_exports_workflow_and_returns_to_terminal(
    cli_connection, session, project, monkeypatch
):
    session.backend = "external"
    monkeypatch.setattr(
        views.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    result = cli_connection("open", "--view", "app")
    assert result.exit_code == 0, result.output
    assert (project.runtime / "workflows/klein-4b_app.json").is_file()


@pytest.mark.parametrize("open_flag", [None, "--open", "--no-open"])
def test_start_reuses_cli_profiles_and_opens_browser_only_when_enabled(
    project, session, monkeypatch, open_flag
):
    calls = []

    def start(bound, backend, endpoint, username, open_browser, echo, foreground, **kwargs):
        assert bound.settings().profiles == ("klein-4b", "klein-4b-edit")
        assert open_browser is (open_flag == "--open")
        assert foreground is None
        calls.append("connected")

    monkeypatch.setattr(services.supervisor, "start", start)
    arguments = ["--project", str(project.root), "start", "--model", "klein-4b"]
    if open_flag:
        arguments.append(open_flag)
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert calls == ["connected"]


@pytest.mark.parametrize("primary", [False, True])
def test_external_cleanup_reports_failure_without_replacing_an_existing_error(
    project, client, monkeypatch, primary
):
    patch_session(monkeypatch, "ComfyClient", lambda *a, **k: client)
    monkeypatch.setattr(client, "close", lambda: None)
    calls = []

    def supervise(*args):
        if primary:
            raise KomaquillError(code="storage_failed")
        return True

    def finish(*args):
        calls.append("finish")
        raise KomaquillError(code="collection_failed")

    patch_session(monkeypatch, "supervise", supervise)
    patch_session(monkeypatch, "finish_external", finish)
    with pytest.raises(KomaquillError) as error:
        startup.start_external(project, "http://127.0.0.1:18188", None, None, lambda _: None)
    assert error.value.code == ("storage_failed" if primary else "collection_failed")
    assert calls == ["finish"]
