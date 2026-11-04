"""Verify retained real-model results without resending accepted operations."""

import argparse
import json
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from komaquill.api import Client, ConversationInput, CreationRequest, PrepareAction, SendInput
from komaquill.domain.errors import KomaquillError
from komaquill.interfaces.http.app import create_app

from scripts._support import atomic_json, message
from scripts.compare_llm import source_hashes
from scripts.verify_player_actions import bounded_calls


def verify(output: Path, budget: Path) -> None:
    output.mkdir(exist_ok=False)
    sources = source_hashes(Path("src/backend/src/komaquill"))
    records = []

    def count() -> int:
        return len(json.loads(budget.read_bytes())["calls"])

    with bounded_calls(budget):
        for case in ("speech", "action", "commit", "checkpoint", "http", "draft"):
            workspace = output / case
            api = Client.initialize(workspace)
            start = count()
            identity = uuid4()
            row = {"case": case, "operation_id": str(identity)}
            try:
                if case == "draft":
                    request = CreationRequest(
                        operation_id=identity,
                        kind="character_draft",
                        prompt="Mira is a calm sixty-year-old bookshop owner with silver hair.",
                        locale="en",
                        allow_real_llm=True,
                    )
                    store = api.library.store
                    original = store.save_job

                    def fail_job(job, save=original):
                        if job.state == "complete":
                            raise KomaquillError(code="storage_failed", stage="storage")
                        return save(job)

                    job, created = api.library.accept(request)
                    assert created
                    with patch.object(store, "save_job", fail_job):
                        try:
                            api.library.execute(job.id)
                        except KomaquillError as error:
                            assert error.code == "storage_failed"
                            row["injected_failure"] = error.code.value
                    generated = count()
                    api.close()
                    api = Client(workspace)
                    result = api.library.job(identity)
                    assert result.state == "complete" and result.result is not None
                    assert count() == generated
                else:
                    scene = api.conversations.create(
                        ConversationInput(
                            title="Recovery acceptance",
                            locale="en",
                            user={"name": "Akari"},
                            character={"name": "Mira", "age": "60"},
                            scenario={"place": "Bookshop"},
                        )
                    )
                    request = SendInput(
                        operation_id=identity,
                        expected_revision=0,
                        text="Hello, Mira. How are you today?",
                        action=PrepareAction(pose="Standing") if case != "speech" else None,
                        allow_real_llm=True,
                    )
                    repository = api.conversations.repository
                    if case == "http":
                        row.update(http_check(api, scene.id, request))
                    elif case in {"commit", "checkpoint"}:

                        def fail(*_):
                            raise KomaquillError(code="storage_failed", stage="storage")

                        with patch.object(repository, case, fail):
                            try:
                                api.conversations.send(scene.id, request)
                            except KomaquillError as error:
                                assert error.code == "storage_failed"
                                row["injected_failure"] = error.code.value
                    else:
                        assert api.conversations.send(scene.id, request).state == "complete"
                    generated = count()
                    api.close()
                    api = Client(workspace)
                    api.operations.reconcile()
                    result = api.operations.get(identity)
                    assert result.state == "complete"
                    duplicate, created = api.conversations.accept(scene.id, request)
                    assert not created and duplicate.state == "complete"
                    saved = api.conversations.get(scene.id)
                    assert saved.turn_count == 1
                    assert len(saved.actions) == (0 if case == "speech" else 1)
                    assert count() == generated
                    row.update(turns=saved.turn_count, actions=len(saved.actions))
                row.update(state="passed", llm_calls=count() - start, calls_on_recovery=0)
            except Exception as error:
                row.update(state="failed", exception_type=type(error).__name__)
                raise
            finally:
                api.close()
                records.append(row)
                atomic_json(
                    output / "results.json",
                    {
                        "verified_at": datetime.now(UTC).isoformat(),
                        "records": records,
                        "source_hashes": sources,
                        "source_unchanged": source_hashes(Path("src/backend/src/komaquill"))
                        == sources,
                    },
                )
                print(json.dumps(row), flush=True)


def http_check(api, scene_id, request) -> dict:
    failed = threading.Event()

    def fail(*_):
        failed.set()
        raise KomaquillError(code="storage_failed", stage="storage")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(api), log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    reads = 0
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as http:
            with patch.object(api.conversations.repository, "commit", fail):
                accepted = http.post(
                    f"/api/v1/conversations/{scene_id}/turns", json=request.model_dump(mode="json")
                )
                assert accepted.status_code == 202
                # Discard the receipt as an interrupted client would; keep the same operation ID.
                for _ in range(100):
                    assert http.get(f"/api/v1/operations/{request.operation_id}").status_code == 200
                    reads += 1
                    time.sleep(0.1)
                assert failed.wait(180)
            for _ in range(50):
                response = http.get(f"/api/v1/operations/{request.operation_id}")
                assert response.status_code == 200 and response.json()["state"] == "complete"
                reads += 1
            return {"http_reads": reads, "http_errors": 0, "accepted_status": 202}
    finally:
        server.should_exit = True
        thread.join(20)
        listener.close()
        assert not thread.is_alive()


def main() -> None:
    parser = argparse.ArgumentParser(description=message("recovery.description"))
    parser.add_argument(
        "--allow-real-llm", action="store_true", required=True, help=message("review.allow_llm")
    )
    parser.add_argument("--output", type=Path, required=True, help=message("efficiency.output"))
    parser.add_argument("--budget", type=Path, required=True, help=message("recovery.budget"))
    args = parser.parse_args()
    root = (Path.cwd() / ".artifacts").resolve()
    if not all(path.resolve().is_relative_to(root) for path in (args.output, args.budget)):
        parser.error(message("recovery.output"))
    verify(args.output.resolve(), args.budget.resolve())


if __name__ == "__main__":
    main()
