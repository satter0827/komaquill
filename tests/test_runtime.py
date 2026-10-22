"""起動監視、外部接続と終了時の回収を検証する。"""

from uuid import uuid4

import pytest

from komaquill import runtime
from komaquill.errors import KomaquillError
from komaquill.service import new_run
from komaquill.storage import atomic_json, load_runs, load_session, save_run
from komaquill.workflow import resource_json


def test_second_start_is_rejected_before_allocation(project, monkeypatch):
    class ForbiddenColab:
        def __init__(self, project):
            pytest.fail("二重起動でColabを操作してはいけない")

    monkeypatch.setattr(runtime, "Colab", ForbiddenColab)
    # 同じスレッドの再入ではなく別のロックインスタンスで競合させる。
    with runtime.monitor_lock(project), pytest.raises(KomaquillError, match="すでに start"):
        runtime.start(project, "colab", None, None, None, lambda message: None)


def test_external_uses_only_standard_api_and_saves_new_browser_job(project, client, monkeypatch):
    original_watch = runtime.watch
    old_id = client.submit(resource_json("workflow-api.json"), {}, "old-browser")
    monkeypatch.setattr(runtime, "ComfyClient", lambda *args, **kwargs: client)

    def watch(project, client, session, colab, echo):
        assert colab is None
        assert old_id in session.excluded_prompt_ids
        client.submit(resource_json("workflow-api.json"), {}, "new-browser")
        atomic_json(
            project.runtime / "stop.json", {"runtime_id": str(session.runtime_id), "force": True}
        )
        original_watch(project, client, session, None, echo)

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
    atomic_json(project.runtime / "stop.json", {"runtime_id": str(uuid4()), "force": True})
    assert runtime.stop_request(project, session) is None


def test_stop_after_monitor_crash_collects_before_stopping_colab(
    project, client, session, monkeypatch
):
    import types
    from contextlib import contextmanager

    client.submit(resource_json("workflow-api.json"), {}, "browser")
    atomic_json(project.session_file, session.model_dump(mode="json"))

    @contextmanager
    def connection(project):
        yield client, session

    class OwnedColab:
        def __init__(self, project):
            pass

        def receipt(self):
            return types.SimpleNamespace(runtime_id=session.runtime_id)

        def stop(self):
            assert load_runs(project)[0].state == "saved"

    monkeypatch.setattr(runtime, "connected", connection)
    monkeypatch.setattr(runtime, "Colab", OwnedColab)
    runtime.stop(project, True, lambda message: None)
