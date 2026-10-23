"""Exercise supervisor ownership, cancellation and independent process lifetime."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.domain.monitor import MonitorState
from komaquill.infrastructure import monitor_state as state
from komaquill.infrastructure import supervisor
from komaquill.interfaces import session_worker


def snapshot(project, **values):
    return MonitorState(
        operation_id=uuid4(),
        backend="external",
        settings=project.settings(),
        endpoint="http://127.0.0.1:9999",
        created_at=datetime.now(UTC),
        observed_at=datetime.now(UTC),
        **values,
    )


@pytest.mark.parametrize(
    "case", ["absent", "zero", "terminal", "pid_reused", "unrelated", "gone", "denied", "live"]
)
def test_process_identity_does_not_trust_a_pid_alone(project, monkeypatch, case):
    row = (
        None
        if case == "absent"
        else snapshot(
            project,
            pid=0 if case == "zero" else 100,
            process_started_at=1.0,
            phase="failed" if case == "terminal" else "ready",
        )
    )

    def process(pid):
        if case == "gone":
            raise psutil.NoSuchProcess(pid)
        if case == "denied":
            raise psutil.AccessDenied(pid)
        return SimpleNamespace(
            create_time=lambda: 2.0 if case == "pid_reused" else 1.0,
            cmdline=lambda: (
                ["unrelated"] if case == "unrelated" else ["komaquill.interfaces.session_worker"]
            ),
            is_running=lambda: True,
            status=lambda: psutil.STATUS_RUNNING,
        )

    monkeypatch.setattr(state.psutil, "Process", process)
    assert state.alive(row) is (case == "live")


def test_snapshot_updates_are_atomic_and_process_scoped(project):
    assert state.read_monitor(project) is None
    state.update_monitor(project, phase="ready")
    row = snapshot(project, pid=os.getpid() + 1)
    state.write_monitor(project, row)
    state.update_monitor(project, phase="ready")
    assert state.read_monitor(project).phase == "starting"
    row.pid = os.getpid()
    state.write_monitor(project, row)
    state.update_monitor(project, phase="ready")
    assert state.read_monitor(project).phase == "ready"
    state.request_cancel(project, row.operation_id, False)
    assert state.cancel_requested(project) is False
    state.request_cancel(project, row.operation_id, True)
    state.request_cancel(project, row.operation_id, False)
    assert state.cancel_requested(project) is True
    with pytest.raises(KomaquillError):
        state.check_cancel(project)
    with pytest.raises(KomaquillError):
        state.request_cancel(project, uuid4(), False)
    row.operation_id = uuid4()
    state.write_monitor(project, row)
    assert state.cancel_requested(project) is None


@pytest.mark.parametrize(
    "case", ["ready", "failed", "stopped", "changed", "missing", "dead", "timeout", "interrupt"]
)
def test_waiting_parent_never_allocates_or_ignores_cancellation(project, monkeypatch, case):
    row = snapshot(project, pid=1, phase="ready" if case == "ready" else "starting")
    if case == "failed":
        row.phase = "failed"
        from komaquill.domain.errors import FailureInfo

        row.failure = FailureInfo.model_validate(
            {
                "code": "connection_failed",
                "stage": "connection",
                "exception_type": "KomaquillError",
            }
        )
    if case == "stopped":
        row.phase = "stopped"
    state.write_monitor(project, row)
    monkeypatch.setattr(supervisor, "alive", lambda _: case != "dead")
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 0 if case == "interrupt" else 99999)
    if case == "timeout":
        project = project.configured(
            project.settings().model_copy(update={"preparation_timeout": 0.0001})
        )
        ticks = iter([0, 1])
        monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(ticks))
    if case == "missing":
        monkeypatch.setattr(supervisor, "read_monitor", lambda _: None)
    if case == "interrupt":
        monkeypatch.setattr(
            supervisor.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        monkeypatch.setattr(supervisor, "wait_stopped", lambda *args: None)
    operation = uuid4() if case == "changed" else row.operation_id
    if case == "ready":
        assert supervisor.wait_ready(project, operation, lambda _: None).phase == "ready"
    else:
        with pytest.raises(KomaquillError):
            supervisor.wait_ready(project, operation, lambda _: None)
    if case in {"timeout", "interrupt"}:
        assert state.cancel_requested(project) is False


@pytest.mark.parametrize(
    "case",
    ["new", "reuse", "mismatch", "stale", "foreground", "foreground_failed", "browser_failed"],
)
def test_start_reuses_only_the_same_session(project, monkeypatch, case):
    row = snapshot(project, pid=10, phase="starting")
    if case in {"reuse", "mismatch", "stale", "browser_failed"}:
        if case == "mismatch":
            row.endpoint = "http://127.0.0.1:9998"
        state.write_monitor(project, row)
    monkeypatch.setattr(
        supervisor, "alive", lambda _: case in {"reuse", "mismatch", "browser_failed"}
    )
    calls = []
    monkeypatch.setattr(
        supervisor, "spawn", lambda _: calls.append("spawn") or SimpleNamespace(pid=os.getpid())
    )

    def ready(*args):
        result = state.read_monitor(project)
        result.browser_opened = case != "browser_failed"
        return result

    monkeypatch.setattr(supervisor, "wait_ready", ready)
    monkeypatch.setattr(supervisor, "open_view", lambda *a, **k: calls.append("open"))
    monkeypatch.setattr(
        supervisor, "load_session", lambda _: SimpleNamespace(endpoint=row.endpoint)
    )
    foreground = (
        (lambda _: int(case == "foreground_failed")) if case.startswith("foreground") else None
    )
    if case in {"mismatch", "foreground_failed", "browser_failed"}:
        with pytest.raises(KomaquillError):
            supervisor.start(
                project, "external", "http://127.0.0.1:9999", None, True, lambda _: None, foreground
            )
    else:
        supervisor.start(
            project, "external", "http://127.0.0.1:9999", None, True, lambda _: None, foreground
        )
    assert ("spawn" in calls) is (case in {"new", "stale"})


@pytest.mark.parametrize("case", ["success", "failure", "dead", "timeout", "interrupt", "changed"])
def test_stop_waiter_reports_failure_and_escalates_only_on_interrupt(project, monkeypatch, case):
    row = snapshot(project, pid=1, phase="stopped" if case == "success" else "starting")
    state.write_monitor(project, row)
    if case == "failure":
        from komaquill.domain.errors import failure_info

        row.phase = "failed"
        row.failure = failure_info(KomaquillError(code="storage_failed"), "storage")
        state.write_monitor(project, row)
    monkeypatch.setattr(supervisor, "alive", lambda _: case != "dead")
    if case == "changed":
        row.operation_id = uuid4()
    ticks = iter([0, 10000])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(ticks))
    if case == "interrupt":

        def pause(_):
            current = state.read_monitor(project)
            current.phase = "stopped"
            state.write_monitor(project, current)
            raise KeyboardInterrupt

        monkeypatch.setattr(supervisor.time, "sleep", pause)
        monkeypatch.setattr(supervisor.time, "monotonic", lambda: 0)
    if case in {"success", "interrupt"}:
        supervisor.wait_stopped(project, row, lambda _: None)
    else:
        with pytest.raises(KomaquillError):
            supervisor.wait_stopped(project, row, lambda _: None)
    if case == "interrupt":
        assert state.cancel_requested(project) is True


def test_stop_and_log_reader_handle_absent_and_active_sessions(project, monkeypatch):
    assert supervisor.stop(project, False, lambda _: None) is False
    row = snapshot(project, pid=1)
    state.write_monitor(project, row)
    monkeypatch.setattr(supervisor, "alive", lambda _: True)
    monkeypatch.setattr(supervisor, "wait_stopped", lambda *args: None)
    assert supervisor.stop(project, True, lambda _: None)
    folder = project.artifacts / "logs"
    folder.mkdir(exist_ok=True)
    (folder / "cli.jsonl").write_text('{"operation":"test"}\ninvalid\n[]\n')
    (folder / "ignored.jsonl").write_text('{"operation":"private"}')
    assert list(supervisor.events(project, False)) == [{"operation": "test"}]
    monkeypatch.setattr(
        supervisor.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    iterator = supervisor.events(project, True)
    assert next(iterator)["operation"] == "test"
    with pytest.raises(KeyboardInterrupt):
        next(iterator)


@pytest.mark.parametrize(
    "case", ["ready", "cancel", "failure", "credentials_missing", "credentials_present", "absent"]
)
def test_worker_freezes_settings_and_saves_terminal_state(project, monkeypatch, case):
    if case != "absent":
        row = snapshot(
            project, username="user" if case.startswith("credentials") else None, open_browser=False
        )
        state.write_monitor(project, row)
    monkeypatch.setattr(
        session_worker.psutil, "Process", lambda: SimpleNamespace(create_time=lambda: 1.0)
    )
    monkeypatch.setattr(
        session_worker.keyring,
        "get_password",
        lambda *a: None if case == "credentials_missing" else "test-value",
    )

    def run(bound, backend, endpoint, username, password, echo, ready):
        assert bound.effective_settings is not None
        if case in {"cancel", "failure"}:
            raise KomaquillError(code="stopping" if case == "cancel" else "connection_failed")
        ready(SimpleNamespace(runtime_id=uuid4()))

    monkeypatch.setattr(session_worker.runtime, "start", run)
    assert session_worker.main(project.root) == (
        1 if case in {"failure", "credentials_missing", "absent"} else 0
    )
    if case != "absent":
        assert state.read_monitor(project).phase in {"stopped", "failed"}


def test_spawn_detaches_and_keeps_credentials_out_of_arguments(project, monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(),
    )
    monkeypatch.setenv("DEBUGPY_TEST", "do-not-inherit")
    supervisor.spawn(project)
    args, options = calls[0]
    assert options["stdin"] == supervisor.subprocess.DEVNULL and options["close_fds"]
    assert "DEBUGPY_TEST" not in options["env"]
    assert args[-1] == str(project.root)
    assert options.get("creationflags") or options.get("start_new_session")


@pytest.mark.parametrize("case", ["valid", "flow", "failure"])
def test_authentication_uses_the_official_flow_before_detaching(project, monkeypatch, case):
    calls = []

    def identity(action):
        calls.append(action)
        if case != "valid" and len(calls) == 1:
            raise KomaquillError(code="authentication_failed")

    monkeypatch.setattr(
        supervisor,
        "Colab",
        lambda _: SimpleNamespace(command=lambda *a: None, provider_call=identity),
    )

    def credentials(*args):
        if case == "failure":
            raise ValueError("do-not-log")

    monkeypatch.setattr(
        supervisor.importlib,
        "import_module",
        lambda _: SimpleNamespace(get_credentials=credentials),
    )
    if case == "failure":
        with pytest.raises(KomaquillError):
            supervisor.authenticate(project)
    else:
        supervisor.authenticate(project)
    assert calls == (
        ["identity"] if case == "valid" or case == "failure" else ["identity", "identity"]
    )


@pytest.mark.parametrize("case", ["valid", "invalid", "missing", "transport"])
def test_progress_is_allowlisted_and_temporary_copies_are_removed(project, monkeypatch, case):
    from komaquill.infrastructure.colab import Colab, Receipt

    control = Colab(project)
    receipt = Receipt(name="test", runtime_id=uuid4())

    def download(action, source, target, **kwargs):
        if case == "transport":
            raise KomaquillError(code="colab_failed")
        if case == "missing":
            return
        from pathlib import Path

        Path(target).write_text(
            json.dumps({"phase": "dependencies" if case == "valid" else "untrusted", "asset": None})
        )

    monkeypatch.setattr(control, "provider_call", download)
    assert control.progress(receipt) == (
        {"phase": "dependencies", "asset": None} if case == "valid" else {}
    )
    assert not list((project.artifacts / "tmp").glob("progress-*.json"))


def test_corrupt_monitor_does_not_block_provider_shutdown(project):
    project.state.mkdir(parents=True)
    (project.state / "monitor.json").write_text("{broken")
    assert supervisor.stop(project, False, lambda _: None) is False
