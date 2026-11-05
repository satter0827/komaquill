"""Verify opt-in evidence collection records failure before resource cleanup."""

import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.api import KomaquillError
from komaquill.domain.conversations.models import Conversation, ConversationInput, Operation
from komaquill.domain.status import SessionStatus

from scripts.evaluation.images import roleplay as review

pytestmark = pytest.mark.usefixtures("scripted_services")


@pytest.mark.parametrize("failed", [False, True])
def test_turn_collection_saves_the_actual_operation_and_snapshot_without_retry(tmp_path, failed):
    identity, operation_id = uuid4(), uuid4()
    conversation = Conversation(id=identity, definition=ConversationInput(title="Saved"))
    operation = Operation(
        id=operation_id,
        conversation_id=identity,
        state="failed" if failed else "complete",
        error="llm_unavailable" if failed else None,
    )
    sent = []
    api = SimpleNamespace(
        conversations=SimpleNamespace(
            send=lambda *args: sent.append(args) or operation,
            get=lambda _: conversation,
            overview=lambda _: conversation,
        )
    )
    report = {"turns": []}
    path = tmp_path / "evidence.json"
    if failed:
        with pytest.raises(KomaquillError, match="llm_unavailable"):
            review.collect_turn(api, identity, "en", "Question", report, path)
    else:
        review.collect_turn(api, identity, "en", "Question", report, path)
    saved = json.loads(path.read_bytes())
    assert len(sent) == 1 and sent[0][1].allow_real_llm
    assert saved["turns"][0]["conversation"]["id"] == str(identity)
    assert saved["turns"][0]["operation"]["state"] == operation.state


@pytest.mark.parametrize(
    "phase,failed,shutdown",
    [
        ("conversations", False, "stopped"),
        ("conversations", True, "stopped"),
        ("images", False, "stopped"),
        ("images", True, "stopped"),
        ("images", False, "unknown"),
        ("images", False, "exception"),
    ],
)
def test_review_finalizes_evidence_and_stops_only_the_runtime_it_attempted_to_start(
    tmp_path, monkeypatch, phase, failed, shutdown
):
    calls = []
    from datetime import UTC, datetime

    from komaquill.api import AccountUsage

    usage = AccountUsage(
        remaining_compute_units=100,
        consumption_rate_hourly=2,
        assignments_count=0,
        observed_at=datetime.now(UTC),
        account_sha256="a" * 64,
    )
    workspace = tmp_path / ".artifacts/previous/workspace"
    workspace.mkdir(parents=True)
    (workspace / "komaquill.toml").write_text('models=["wai-illustrious"]\n', encoding="utf-8")
    (workspace.parent / "evidence.json").write_text(
        json.dumps({"selections": [], "input_sha256": "parent"})
    )

    def stop():
        calls.append("stop")
        if shutdown == "exception":
            raise RuntimeError("Injected shutdown failure")
        return SessionStatus(gpu=shutdown)

    api = SimpleNamespace(
        workspace=workspace,
        limit_llm_calls=lambda *args, **kw: None,
        close=lambda: None,
        runtime=SimpleNamespace(
            usage=lambda: usage,
            status=lambda: SessionStatus(gpu="absent"),
            start=lambda **kwargs: calls.append("start") or SessionStatus(gpu="active"),
            stop=stop,
        ),
    )

    def initialized(path):
        path.mkdir(parents=True)
        api.workspace = path
        return api

    monkeypatch.setattr(
        review, "Client", SimpleNamespace(initialize=initialized, __call__=lambda _: api)
    )
    if phase == "images":
        monkeypatch.setattr(review, "Client", lambda _: api)
    monkeypatch.setattr(review, "ROOT", tmp_path)
    monkeypatch.setattr(review, "input_fingerprint", lambda _: "fixed")
    monkeypatch.setattr(review, "source_revision", lambda _: "test")

    def collect(*_):
        calls.append("collect")
        if failed:
            raise KomaquillError(code="generation_failed")

    monkeypatch.setattr(review, "conversations", collect)
    monkeypatch.setattr(review, "images", collect)
    args = ["review", "--allow-real-llm", "--phase", phase]
    if phase == "images":
        args += ["--allow-gpu", "--workspace", str(workspace)]
    monkeypatch.setattr(sys, "argv", args)
    expected_failure = failed or shutdown != "stopped"
    if expected_failure:
        with pytest.raises(KomaquillError):
            review.main()
    else:
        review.main()
    report = json.loads(
        next((tmp_path / ".artifacts/records/reports").glob("*/evidence.json")).read_bytes()
    )
    assert report["functional_status"] == ("failed" if expected_failure else "passed")
    assert report["review_status"] == "pending" and report["inputs_unchanged"]
    assert calls == (["start", "collect", "stop"] if phase == "images" else ["collect"])


def test_real_collection_requires_explicit_permission(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["review"])
    with pytest.raises(SystemExit):
        review.main()


def test_preexisting_runtime_is_never_started_or_stopped(tmp_path, monkeypatch):
    workspace = tmp_path / ".artifacts/previous/workspace"
    state = workspace / ".artifacts/state"
    state.mkdir(parents=True)
    (state / "session.json").write_text("existing ownership", encoding="utf-8")
    (workspace.parent / "evidence.json").write_text(
        json.dumps({"selections": [], "input_sha256": "parent"}), encoding="utf-8"
    )
    calls = []
    from datetime import UTC, datetime

    from komaquill.api import AccountUsage

    usage = AccountUsage(
        remaining_compute_units=100,
        consumption_rate_hourly=2,
        assignments_count=0,
        observed_at=datetime.now(UTC),
        account_sha256="a" * 64,
    )
    api = SimpleNamespace(
        workspace=workspace,
        limit_llm_calls=lambda *args, **kw: None,
        close=lambda: None,
        runtime=SimpleNamespace(
            usage=lambda: usage,
            status=lambda: SessionStatus(gpu="active", connection="connected"),
            start=lambda **kwargs: calls.append("start"),
            stop=lambda: calls.append("stop"),
        ),
    )
    monkeypatch.setattr(review, "ROOT", tmp_path)
    monkeypatch.setattr(review, "Client", lambda _: api)
    monkeypatch.setattr(review, "input_fingerprint", lambda _: "fixed")
    monkeypatch.setattr(review, "source_revision", lambda _: "test")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review",
            "--allow-real-llm",
            "--allow-gpu",
            "--phase",
            "images",
            "--workspace",
            str(workspace),
        ],
    )
    with pytest.raises(KomaquillError, match="operation_busy"):
        review.main()
    assert calls == []
    assert (state / "session.json").read_text(encoding="utf-8") == "existing ownership"


def test_failed_image_acceptance_is_reported_without_waiting_for_a_missing_run(tmp_path):
    from komaquill.domain.library.models import CreationJob, CreationRequest

    request = CreationRequest(
        llm_profile="fake", operation_id=uuid4(), kind="portrait", target_id=uuid4()
    )
    job = CreationJob(
        id=request.operation_id,
        request=request,
        original="source",
        state="failed",
        error="stopping",
    )
    api = SimpleNamespace(library=SimpleNamespace(job=lambda _: job))
    record = tmp_path / "evidence.json"
    with pytest.raises(KomaquillError, match="stopping"):
        review.save_image(api, job.id, "character", {}, record)
    assert json.loads(record.read_bytes())["failed_jobs"][0]["error"] == "stopping"
