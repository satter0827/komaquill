"""Test interrupted monitoring and malformed data without hiding other requests."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from komaquill import runtime
from komaquill.errors import KomaquillError
from komaquill.service import collect, new_run
from komaquill.storage import load_runs, save_run, stop_request
from komaquill.workflow import resource_json


def test_invalid_folder_does_not_block_other_runs_or_get_overwritten(project, client, session):
    broken = project.runs / "unexpected-folder"
    broken.mkdir(parents=True)
    original = b"original-invalid-record"
    (broken / "run.json").write_bytes(original)
    client.submit(resource_json("workflow-api.json"), {}, "healthy")
    report = collect(project, client, session)
    assert report.saved == 1 and len(report.errors) == 1
    assert "unexpected-folder" in str(report.errors[0])
    assert (broken / "run.json").read_bytes() == original
    with pytest.raises(KomaquillError, match="フォルダー"):
        load_runs(project)


def test_watch_records_disconnect_once_and_recovers_without_new_allocation(
    project, client, session
):
    attempts = []
    messages = []

    def reconnect():
        attempts.append(True)
        if len(attempts) == 1:
            raise KomaquillError("temporary", code="connection_failed")
        client.submit(resource_json("workflow-api.json"), {}, "recovered")
        runtime.request_stop(project, session, True)

    assert runtime.watch(project, client, session, messages.append, reconnect) is True
    assert any("再接続" in text for text in messages)
    assert any("保存しました" in text for text in messages)
    events = [
        json.loads(line)
        for line in (project.artifacts / "logs" / str(session.runtime_id) / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert sum(row["result"] == "disconnected" for row in events) == 1
    assert sum(row["result"] == "restored" for row in events) == 1


def test_watch_interrupt_requests_normal_shutdown(project, client, session):
    def interrupt():
        raise KeyboardInterrupt

    assert runtime.watch(project, client, session, lambda _: None, interrupt) is False
    assert stop_request(project, session) is False


def test_repeated_collection_errors_are_not_spammed(project, client, session, stub, monkeypatch):
    client.submit(resource_json("workflow-api.json"), {}, "broken")
    stub.corrupt_image = True
    sleeps = []

    def sleep(_):
        sleeps.append(True)
        if len(sleeps) == 2:
            runtime.request_stop(project, session, True)

    monkeypatch.setattr(runtime.time, "sleep", sleep)
    messages = []
    runtime.watch(project, client, session, messages.append)
    assert sum("再回収" in text for text in messages) == 1


def test_drain_returns_connectivity_failure_and_preserves_pending_request(
    project, client, session, monkeypatch
):
    run = new_run(project, session, uuid4(), {})
    save_run(project, run)

    def fail(*args):
        raise KomaquillError("private-body", code="connection_failed")

    monkeypatch.setattr(runtime, "collect", fail)
    report = runtime.drain(project, client, session, False, lambda _: None)
    assert report.pending_ids == (run.run_id,)
    assert report.failure.code == "connection_failed"
    assert "private-body" not in report.failure.model_dump_json()


def test_second_interrupt_switches_drain_to_force(project, client, session, monkeypatch):
    original = runtime.collect
    attempts = []

    def interrupt(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise KeyboardInterrupt
        return original(*args)

    monkeypatch.setattr(runtime, "collect", interrupt)
    runtime.drain(project, client, session, False, lambda _: None)
    assert stop_request(project, session) is True


def test_external_shutdown_keeps_unfinished_requests_recoverable(project, client, session, stub):
    session.backend = "external"
    prompt = client.submit(resource_json("workflow-api.json"), {}, "unfinished")
    stub.history[prompt]["status"]["completed"] = False
    runtime.finish_external(project, client, session, True, lambda _: None)
    run = load_runs(project)[0]
    assert run.state == "queued" and run.pending
    report = json.loads(
        (project.artifacts / "logs" / str(session.runtime_id) / "shutdown.json").read_bytes()
    )
    assert report["gpu"] == "not_managed" and report["pending_ids"] == [str(run.run_id)]


def test_quality_entrypoint_returns_tool_failure_without_traceback(tmp_path, monkeypatch, capsys):
    import subprocess

    from scripts import check

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(7, args[0])

    monkeypatch.setattr(check.subprocess, "run", fail)
    namespace = {"__name__": "__main__", "__file__": str(tmp_path / "scripts/check.py")}
    with pytest.raises(SystemExit) as error:
        exec(
            compile(Path(check.__file__).read_text(encoding="utf-8"), check.__file__, "exec"),
            namespace,
        )
    assert error.value.code == 7 and "品質検査" in capsys.readouterr().err
