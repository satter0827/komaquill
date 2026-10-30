"""Test interrupted monitoring and malformed data without hiding other requests."""

import json
from uuid import uuid4

import pytest
from komaquill.adapters.storage import load_runs, save_run, stop_request
from komaquill.adapters.workflows import resource_json
from komaquill.domain.errors import KomaquillError
from komaquill.images.generation import collect, new_run
from komaquill.runtime import connection, monitor, shutdown, startup

from tests.support.session_controls import patch_session


def test_invalid_folder_does_not_block_other_runs_or_get_overwritten(project, client, session):
    broken = project.runs / "unexpected-folder"
    broken.mkdir(parents=True)
    original = b"original-invalid-record"
    from komaquill.adapters.database import database, images

    with database(project.data / "komaquill.sqlite3").engine.begin() as connection:
        connection.execute(
            images.insert().values(id="unexpected-folder", payload=original.decode())
        )
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "healthy")
    report = collect(project, client, session)
    assert report.saved == 1 and len(report.errors) == 1
    assert "unexpected-folder" in str(report.errors[0])
    from sqlalchemy import select

    with database(project.data / "komaquill.sqlite3").engine.connect() as connection:
        assert (
            connection.execute(
                select(images.c.payload).where(images.c.id == "unexpected-folder")
            ).scalar_one()
            == original.decode()
        )
    with pytest.raises(KomaquillError):
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
        client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "recovered")
        connection.request_stop(project, session, True)

    assert monitor.watch(project, client, session, messages.append, reconnect) is True
    assert any("Reconnected" in text for text in messages)
    assert any("Saved:" in text for text in messages)
    events = [
        json.loads(line)
        for line in (project.logs / str(session.runtime_id) / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert sum(row["result"] == "disconnected" for row in events) == 1
    assert sum(row["result"] == "restored" for row in events) == 1


def test_watch_interrupt_requests_normal_shutdown(project, client, session):
    def interrupt():
        raise KeyboardInterrupt

    assert monitor.watch(project, client, session, lambda _: None, interrupt) is False
    assert stop_request(project, session) is False


def test_repeated_collection_errors_are_not_spammed(project, client, session, stub, monkeypatch):
    client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "broken")
    stub.corrupt_image = True
    sleeps = []

    def sleep(_):
        sleeps.append(True)
        if len(sleeps) == 2:
            connection.request_stop(project, session, True)

    monkeypatch.setattr(startup.time, "sleep", sleep)
    messages = []
    monitor.watch(project, client, session, messages.append)
    assert sum("collect" in text for text in messages) == 1


def test_drain_returns_connectivity_failure_and_preserves_pending_request(
    project, client, session, monkeypatch
):
    run = new_run(project, session, uuid4(), {})
    save_run(project, run)

    def fail(*args):
        raise KomaquillError("private-body", code="connection_failed")

    patch_session(monkeypatch, "collect", fail)
    report = monitor.drain(project, client, session, False, lambda _: None)
    assert report.pending_ids == (run.run_id,)
    assert report.failure.code == "connection_failed"
    assert "private-body" not in report.failure.model_dump_json()


def test_second_interrupt_switches_drain_to_force(project, client, session, monkeypatch):
    original = monitor.collect
    attempts = []

    def interrupt(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise KeyboardInterrupt
        return original(*args)

    patch_session(monkeypatch, "collect", interrupt)
    monitor.drain(project, client, session, False, lambda _: None)
    assert stop_request(project, session) is True


def test_external_shutdown_keeps_unfinished_requests_recoverable(project, client, session, stub):
    session.backend = "external"
    prompt = client.submit(resource_json("presets/klein-4b/workflow-api.json"), {}, "unfinished")
    stub.history[prompt]["status"]["completed"] = False
    shutdown.finish_external(project, client, session, True, lambda _: None)
    run = load_runs(project)[0]
    assert run.state == "queued" and run.pending
    report = json.loads((project.logs / str(session.runtime_id) / "shutdown.json").read_bytes())
    assert report["gpu"] == "not_managed" and report["pending_ids"] == [str(run.run_id)]
