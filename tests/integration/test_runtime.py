"""Test monitoring, external connections and shutdown collection."""

from uuid import uuid4

import pytest

from komaquill.application.generation import new_run
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import runtime as runtime
from komaquill.infrastructure.storage import atomic_json, load_runs, load_session, save_run
from komaquill.infrastructure.workflows import resource_json
from tests.support.provider import stopped as confirmed_stop


def test_second_start_is_rejected_before_allocation(project, monkeypatch):
    class ForbiddenColab:
        def __init__(self, project):
            pytest.fail("二重起動でColabを操作してはいけない")

    monkeypatch.setattr(runtime, "Colab", ForbiddenColab)
    # Use a separate instance to test contention instead of reentrancy.
    with runtime.monitor_lock(project), pytest.raises(KomaquillError):
        runtime.start(project, "colab", None, None, None, lambda message: None)


def test_external_uses_only_standard_api_and_saves_new_browser_job(project, client, monkeypatch):
    original_watch = runtime.watch
    old_id = client.submit(resource_json("workflow-api.json"), {}, "old-browser")
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)

    def watch(project, client, session, echo):
        assert old_id in session.excluded_prompt_ids
        client.submit(resource_json("workflow-api.json"), {}, "new-browser")
        atomic_json(
            project.state / "stop.json", {"runtime_id": str(session.runtime_id), "force": True}
        )
        return original_watch(project, client, session, echo)

    monkeypatch.setattr(runtime, "watch", watch)
    runtime.start_external(project, "http://127.0.0.1:8188", None, None, lambda message: None)
    assert len(load_runs(project)) == 1
    assert load_runs(project)[0].state == "saved"
    session = load_session(project)
    assert session.source_revision is None
    assert session.model_sha256 is None


def test_force_shutdown_collects_completed_results_without_waiting(
    project, client, session, monkeypatch
):
    client.submit(resource_json("workflow-api.json"), {}, "browser")

    def no_wait(seconds):
        pytest.fail("強制終了で生成完了を待ってはいけない")

    monkeypatch.setattr(runtime.time, "sleep", no_wait)
    runtime.drain(project, client, session, True, lambda message: None)
    assert load_runs(project)[0].state == "saved"


def test_shutdown_timeout_reports_unresolved_run(project, client, session, monkeypatch):
    run = new_run(project, session, uuid4(), {})
    run.state = "unknown"
    save_run(project, run)
    clock = iter([0, 601])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    messages = []
    runtime.drain(project, client, session, False, messages.append)
    assert str(run.run_id) in messages[-1]
    assert load_runs(project)[0].state == "unknown"


def test_stale_stop_request_does_not_stop_new_runtime(project, session):
    atomic_json(project.state / "stop.json", {"runtime_id": str(uuid4()), "force": True})
    assert runtime.stop_request(project, session) is None


def owned_colab(project, session, client, monkeypatch):
    from komaquill.infrastructure.colab import Colab, Receipt

    colab = Colab(project)
    receipt = Receipt(
        name="owned",
        runtime_id=session.runtime_id,
        source_revision="a" * 40,
        endpoint_id="expected",
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(runtime, "Colab", lambda project: colab)
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(colab, "refresh", lambda receipt: True)
    monkeypatch.setattr(colab, "recover_diagnostics", lambda receipt: ())
    monkeypatch.setattr(colab, "cleanup_processes", lambda receipt: None)
    monkeypatch.setattr(colab, "stop", confirmed_stop)
    return colab, receipt


def test_stop_after_monitor_crash_collects_and_recovers_logs_before_stopping(
    project, client, session, monkeypatch
):
    client.submit(resource_json("workflow-api.json"), {}, "browser")
    atomic_json(project.session_file, session.model_dump(mode="json"))
    colab, receipt = owned_colab(project, session, client, monkeypatch)
    steps = []

    def logs(receipt):
        assert load_runs(project)[0].state == "saved"
        steps.append("logs")
        return ()

    def stop():
        assert steps == ["logs"]
        steps.append("stop")
        return confirmed_stop()

    monkeypatch.setattr(colab, "recover_diagnostics", logs)
    monkeypatch.setattr(colab, "stop", stop)
    runtime.stop(project, True, lambda message: None)
    assert steps == ["logs", "stop"]
    assert colab.receipt() is None


def test_force_stop_request_is_never_downgraded(project, session):
    runtime.request_stop(project, session, True)
    runtime.request_stop(project, session, False)
    assert runtime.stop_request(project, session) is True


def test_stop_does_not_end_replacement_runtime(project, session, monkeypatch):
    from types import SimpleNamespace

    from filelock import Timeout

    atomic_json(project.session_file, session.model_dump(mode="json"))

    class ReplacedLock:
        calls = 0

        def acquire(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise Timeout("monitor")

        def release(self):
            pass

    class ReplacedColab:
        reads = 0

        def __init__(self, project):
            pass

        def receipt(self):
            type(self).reads += 1
            return SimpleNamespace(runtime_id=session.runtime_id if self.reads == 1 else uuid4())

        def stop(self):
            pytest.fail("停止要求後に置き換わった環境を終了してはいけない")

    monkeypatch.setattr(runtime, "monitor_lock", lambda project: ReplacedLock())
    monkeypatch.setattr(runtime, "Colab", ReplacedColab)
    with pytest.raises(KomaquillError):
        runtime.stop(project, True, lambda message: None)


def test_external_restart_excludes_offline_jobs_but_recovers_recorded_jobs(
    project, client, session, stub, monkeypatch
):
    from komaquill.application.generation import collect

    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(runtime, "watch", lambda *args: True)
    runtime.start_external(project, session.endpoint, None, None, lambda message: None)
    client.submit(resource_json("workflow-api.json"), {}, "offline-browser")
    runtime.start_external(project, session.endpoint, None, None, lambda message: None)
    assert load_runs(project) == []
    current = load_session(project)
    client.submit(resource_json("workflow-api.json"), {}, "online-browser")
    assert collect(project, client, current).saved == 1


@pytest.mark.parametrize("failure", ["logs", "stop", "cleanup"])
def test_shutdown_failures_do_not_skip_gpu_stop_or_drop_ownership(
    project, client, session, monkeypatch, failure
):
    run = new_run(project, session, uuid4(), {})
    run.state = "queued"
    save_run(project, run)
    colab, receipt = owned_colab(project, session, client, monkeypatch)
    steps = []

    def logs(receipt):
        steps.append("logs")
        if failure == "logs":
            raise KomaquillError("ログの失敗")
        return ()

    def stop():
        steps.append("stop")
        if failure == "stop":
            raise KomaquillError("終了の失敗")
        return confirmed_stop()

    def cleanup(receipt):
        steps.append("cleanup")
        if failure == "cleanup":
            raise KomaquillError("接続の失敗")

    monkeypatch.setattr(colab, "recover_diagnostics", logs)
    monkeypatch.setattr(colab, "stop", stop)
    monkeypatch.setattr(colab, "cleanup_processes", cleanup)
    if failure == "logs":
        with pytest.raises(KomaquillError):
            runtime.finish_colab(
                project, colab, receipt, client, session, True, lambda message: None, []
            )
        assert colab.receipt() is None
        assert load_runs(project)[0].state == "lost"
    else:
        with pytest.raises(KomaquillError):
            runtime.finish_colab(
                project, colab, receipt, client, session, True, lambda message: None, []
            )
        assert colab.receipt() is not None
        assert load_runs(project)[0].state == ("queued" if failure == "stop" else "lost")
    assert steps == ["logs", "stop", "cleanup"]


def test_stop_restores_missing_ssh_without_acquiring_gpu(project, client, session, monkeypatch):
    import types

    colab, receipt = owned_colab(project, session, client, monkeypatch)
    steps = []

    def disconnected(*args):
        raise KomaquillError("切断中")

    monkeypatch.setattr(runtime, "verify_runtime", disconnected)
    monkeypatch.setattr(runtime, "check_port", lambda port: None)
    monkeypatch.setattr(
        colab, "tunnel", lambda receipt: steps.append("ssh") or types.SimpleNamespace()
    )
    monkeypatch.setattr(runtime, "terminate", lambda process, timeout: steps.append("close"))
    runtime.stop(project, True, lambda message: None)
    assert steps == ["ssh", "close"]
    assert colab.receipt() is None


def test_interrupt_during_log_recovery_still_stops_gpu(project, client, session, monkeypatch):
    colab, receipt = owned_colab(project, session, client, monkeypatch)
    stopped = []

    def interrupted(receipt):
        raise KeyboardInterrupt

    monkeypatch.setattr(colab, "recover_diagnostics", interrupted)
    monkeypatch.setattr(colab, "stop", lambda: stopped.append(True) or confirmed_stop())
    with pytest.raises(KomaquillError):
        runtime.finish_colab(
            project, colab, receipt, client, session, True, lambda message: None, []
        )
    assert stopped == [True]
    assert colab.receipt() is None


def test_shutdown_freezes_new_submissions_before_collection(project, client, session, monkeypatch):
    from komaquill.application.generation import generate

    colab, receipt = owned_colab(project, session, client, monkeypatch)

    def drain(*args):
        with pytest.raises(KomaquillError):
            generate(project, client, session, "a cabin", "", 1)

    monkeypatch.setattr(runtime, "drain", drain)
    runtime.finish_colab(project, colab, receipt, client, session, True, lambda message: None, [])
    assert colab.receipt() is None


@pytest.mark.parametrize("phase", ["drain", "logs", "cleanup"])
def test_unexpected_shutdown_error_still_stops_gpu_and_retains_receipt(
    project, client, session, monkeypatch, phase
):
    colab, receipt = owned_colab(project, session, client, monkeypatch)
    stopped = []
    monkeypatch.setattr(colab, "stop", lambda: stopped.append(True) or confirmed_stop())

    def broken(*args):
        raise ValueError("private-response-sentinel")

    if phase == "drain":
        monkeypatch.setattr(runtime, "drain", broken)
    elif phase == "logs":
        monkeypatch.setattr(colab, "recover_diagnostics", broken)
    else:
        monkeypatch.setattr(colab, "cleanup_processes", broken)
    with pytest.raises(KomaquillError) as error:
        runtime.finish_colab(project, colab, receipt, client, session, True, lambda _: None, [])
    assert stopped == [True]
    assert colab.receipt() is not None
    diagnostic = project.artifacts / "logs" / str(receipt.runtime_id) / "shutdown.json"
    assert "private-response-sentinel" not in str(error.value) + diagnostic.read_text()
    assert "ValueError" in diagnostic.read_text()


def test_external_stop_without_monitor_blocks_new_submissions(
    project, client, session, monkeypatch
):
    from komaquill.application.generation import generate

    session.backend = "external"
    atomic_json(project.session_file, session.model_dump(mode="json"))
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)

    def drain(*args):
        with pytest.raises(KomaquillError):
            generate(project, client, session, "a cabin", "", 1)

    monkeypatch.setattr(runtime, "drain", drain)
    runtime.stop(project, True, lambda _: None)


def test_failed_shutdown_output_does_not_skip_gpu_stop(project, client, session, monkeypatch):
    colab, receipt = owned_colab(project, session, client, monkeypatch)
    stopped = []

    def unavailable(*args):
        raise KomaquillError("接続を利用できません")

    def closed_terminal(message):
        raise BrokenPipeError("closed")

    monkeypatch.setattr(runtime, "drain", unavailable)
    monkeypatch.setattr(colab, "stop", lambda: stopped.append(True) or confirmed_stop())
    with pytest.raises(KomaquillError):
        runtime.finish_colab(project, colab, receipt, client, session, True, closed_terminal, [])
    assert stopped == [True]
    assert colab.receipt() is not None


@pytest.mark.parametrize("failure", ["ssh", "session"])
def test_stop_after_monitor_crash_attempts_gpu_stop_when_reconnect_fails(
    project, client, session, monkeypatch, failure
):
    colab, receipt = owned_colab(project, session, client, monkeypatch)
    stopped = []
    monkeypatch.setattr(colab, "stop", lambda: stopped.append(True) or confirmed_stop())
    if failure == "session":
        project.session_file.write_bytes(b"invalid-json")
    else:

        def disconnected(*args):
            raise KomaquillError("SSH接続がありません")

        def unavailable(*args):
            raise FileNotFoundError("ssh")

        monkeypatch.setattr(runtime, "verify_runtime", disconnected)
        monkeypatch.setattr(runtime, "check_port", lambda port: None)
        monkeypatch.setattr(colab, "tunnel", unavailable)
    runtime.stop(project, True, lambda _: None)
    assert stopped == [True]
    assert colab.receipt() is None
