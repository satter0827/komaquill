"""Exercise collection locks while requests are delayed or finalized."""

import threading
from uuid import uuid4

from komaquill.adapters.atomic import atomic_json
from komaquill.images.generation import collect, mark_lost, new_run


def completed(project, session, stub):
    identity = str(uuid4())
    graph = {"3": {"inputs": {"seed": 5}}}
    stub.history[identity] = {
        "prompt": [0, identity, graph, {}, ["9"]],
        "outputs": {
            "9": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}
        },
        "status": {"completed": True},
    }
    return identity


def test_network_wait_does_not_hold_global_lock_and_late_results_cannot_revert_lost(
    project, session, stub, client, monkeypatch
):
    completed(project, session, stub)
    entered, release = threading.Event(), threading.Event()
    original = client.image
    results = []

    def delayed(reference):
        entered.set()
        assert release.wait(5)
        return original(reference)

    monkeypatch.setattr(client, "image", delayed)
    worker = threading.Thread(target=lambda: results.append(collect(project, client, session)))
    worker.start()
    try:
        assert entered.wait(5)
        lost = mark_lost(project, session.runtime_id)
        assert len(lost) == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert project.repository.read(lost[0]).state == "lost"
    assert results[0].saved == 0


def test_parallel_collector_skips_inflight_request_without_duplicate_download(
    project, session, stub, client, monkeypatch
):
    completed(project, session, stub)
    entered, release = threading.Event(), threading.Event()
    original = client.image
    calls = []

    def delayed(reference):
        calls.append(reference)
        entered.set()
        assert release.wait(5)
        return original(reference)

    monkeypatch.setattr(client, "image", delayed)
    worker = threading.Thread(target=lambda: collect(project, client, session))
    worker.start()
    try:
        assert entered.wait(5)
        other = collect(project, client, session)
        assert other.saved == 0 and other.pending == 1
    finally:
        release.set()
        worker.join(5)
    assert len(calls) == 1
    assert project.repository.load()[0].state == "saved"


def test_replaced_session_rejects_old_observations(project, session, client):
    run = new_run(project, session, uuid4(), {})
    project.repository.save(run)
    replacement = session.model_copy(update={"runtime_id": uuid4()})
    atomic_json(project.session_file, replacement.model_dump(mode="json"))
    assert collect(project, client, session).saved == 0
    assert project.repository.read(run.run_id).state == "prepared"
