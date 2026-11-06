"""Check live-browser orchestration without permitting real inference."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from komaquill.api import Client, SendInput
from komaquill.domain.conversations.models import Reply

from scripts.acceptance.conversations import long


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(long, "ROOT", tmp_path)
    monkeypatch.setattr(long, "input_fingerprint", lambda _: "source")
    monkeypatch.setattr(long, "source_revision", lambda _: "revision")
    root = tmp_path / ".artifacts/live"
    calls = []

    def browser(api, output, report, scenario, **kwargs):
        assert api.llm_usage()["reserved_calls"] == 0
        assert kwargs["timeout"] > 0 and kwargs["script"].endswith("long.mjs")
        assert len(scenario["messages"]) == scenario["target_turns"] <= 20
        assert scenario["format_version"] == 1 and scenario["real_llm"] is True
        calls.append(scenario)
        report["browser"] = {"status": "passed"}

    monkeypatch.setattr(long, "browse", browser)
    return root, calls


def test_reopening_uses_the_same_conversation_and_durable_budget(prepared):
    root, calls = prepared
    assert long.collect(root / "first")
    case = (root / "case.json").read_bytes()
    first = json.loads((root / "first/run.json").read_bytes())
    assert long.collect(root / "second")
    second = json.loads((root / "second/run.json").read_bytes())
    assert (root / "case.json").read_bytes() == case
    assert calls[0]["conversation"] == calls[1]["conversation"]
    assert first["budget"] == second["budget"]
    with Client(root / "workspace") as api:
        assert len(api.conversations.list()) == 1


def test_failure_is_recorded_without_copying_private_exception_text(prepared, monkeypatch):
    root, _ = prepared

    def fail(*args, **kwargs):
        raise RuntimeError("private-response-text")

    monkeypatch.setattr(long, "browse", fail)
    assert not long.collect(root / "failed")
    raw = (root / "failed/run.json").read_text()
    assert json.loads(raw)["status"] == "failed"
    assert "private-response-text" not in raw


def test_expired_allowance_does_not_launch_a_browser(prepared, monkeypatch):
    root, calls = prepared

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(hours=2)

    monkeypatch.setattr(long, "datetime", Later)
    assert not long.collect(root / "expired")
    assert calls == []
    assert json.loads((root / "expired/run.json").read_bytes())["budget"]["reserved_calls"] == 0


def test_output_is_confined_to_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(long, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        long.collect(tmp_path / "outside")
    assert not (tmp_path / "outside").exists()


def test_explicit_new_case_keeps_the_same_allowance(prepared):
    root, calls = prepared
    assert long.collect(root / "first")
    first = json.loads((root / "first/run.json").read_bytes())
    assert long.collect(root / "second", new_conversation=True)
    second = json.loads((root / "second/run.json").read_bytes())
    assert calls[0]["conversation"] != calls[1]["conversation"]
    assert first["budget"] == second["budget"]
    assert (root / "cases" / f"{calls[0]['conversation']}.json").is_file()


def test_unconfirmed_acceptance_is_not_submitted_again(prepared):
    root, calls = prepared
    assert long.collect(root / "first")
    with Client(root / "workspace") as api:
        api.conversations.repository.accept(
            UUID(calls[0]["conversation"]),
            SendInput(text="A retained request", operation_id=uuid4(), expected_revision=0),
        )
    assert not long.collect(root / "second")
    assert len(calls) == 1
    result = json.loads((root / "second/run.json").read_bytes())
    assert result["failure"]["code"] == "operation_busy"
    assert result["budget"]["reserved_calls"] == 0


def test_branch_reuses_saved_prefix_without_replacing_the_original_or_budget(prepared):
    root, calls = prepared
    assert long.collect(root / "first")
    identity = UUID(calls[0]["conversation"])
    with Client(root / "workspace") as api:
        repository = api.conversations.repository
        request = SendInput(text="Retained prefix", operation_id=uuid4(), expected_revision=0)
        repository.accept(identity, request)
        assert repository.running(request.operation_id)
        repository.commit(request.operation_id, Reply(text="Saved reply", state_patch={}))
        original = api.conversations.get(identity).model_dump(mode="json")
        budget = api.llm_usage()
    assert long.collect(root / "branch", fork_after_turn=1, through_turn=2)
    assert calls[1]["target_turns"] == 2
    assert calls[1]["conversation"] != str(identity)
    with Client(root / "workspace") as api:
        assert api.llm_usage() == budget
        assert api.conversations.get(identity).model_dump(mode="json") == original
        branched = api.conversations.get(UUID(calls[1]["conversation"]))
        assert branched.turn_count == 1 and branched.turns[0].assistant == "Saved reply"
    assert (root / "cases" / f"{identity}.json").is_file()


@pytest.mark.parametrize("turn", [0, 1, 20])
def test_invalid_branch_preserves_the_current_case(prepared, turn):
    root, calls = prepared
    assert long.collect(root / "first")
    previous = (root / "case.json").read_bytes()
    assert not long.collect(root / "invalid-branch", fork_after_turn=turn)
    assert (root / "case.json").read_bytes() == previous and len(calls) == 1


@pytest.mark.parametrize("turn", [0, 21])
def test_invalid_target_does_not_launch_or_create_a_conversation(prepared, turn):
    root, calls = prepared
    assert not long.collect(root / "invalid-target", through_turn=turn)
    assert calls == [] and not (root / "case.json").exists()


def test_changed_inputs_are_not_reported_as_passed(prepared, monkeypatch):
    root, _ = prepared
    fingerprints = iter(["before", "after"])
    monkeypatch.setattr(long, "input_fingerprint", lambda _: next(fingerprints))
    assert not long.collect(root / "changed")
    assert json.loads((root / "changed/run.json").read_bytes())["status"] == "inputs_changed"


def test_missing_allowance_during_cleanup_still_finalizes_a_failed_report(prepared, monkeypatch):
    root, _ = prepared

    def lose_budget(*args, **kwargs):
        (root / "allowance.sqlite3").unlink()

    monkeypatch.setattr(long, "browse", lose_budget)
    assert not long.collect(root / "budget-lost")
    report = json.loads((root / "budget-lost/run.json").read_bytes())
    assert report["status"] == "failed" and report["finished_at"]
    assert report["budget_failure"]["code"] == "storage_failed"


def test_close_failure_is_recorded_without_private_exception_text(prepared, monkeypatch):
    root, _ = prepared
    original = Client.close

    def close(api):
        original(api)
        raise RuntimeError("private-cleanup-value")

    monkeypatch.setattr(Client, "close", close)
    assert not long.collect(root / "close-failed")
    raw = (root / "close-failed/run.json").read_text()
    result = json.loads(raw)
    assert result["status"] == "failed"
    assert result["cleanup_failure"]["exception_type"] == "RuntimeError"
    assert "private-cleanup-value" not in raw
