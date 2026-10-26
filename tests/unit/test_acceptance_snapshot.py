"""Keep failed acceptance diagnostics useful without copying private payloads."""

import json

import pytest

from tests.support.acceptance import failure_snapshot


def test_failure_snapshot_keeps_only_operational_fields(tmp_path):
    state = tmp_path / ".artifacts/state"
    logs = tmp_path / ".artifacts/records/logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    failure = {"code": "storage_failed", "exception_type": "PermissionError"}
    (state / "monitor.json").write_text(
        json.dumps(
            {"phase": "failed", "failure": {**failure, "message": "private"}, "settings": {}}
        )
    )
    (state / "stop.json").write_text(json.dumps({"force": True, "input": "private"}))
    (logs / "events.jsonl").write_text(
        json.dumps(
            {"operation": "monitor", "result": "failed", "failure": failure, "body": "private"}
        )
        + "\n[]\n"
        + json.dumps({"failure": "private"})
    )
    result = failure_snapshot(tmp_path)
    assert set(result["monitor.json"]) == {"phase", "failure"}
    assert set(result["monitor.json"]["failure"]) == {
        "code",
        "stage",
        "exception_type",
        "http_status",
        "retryable",
    }
    assert result["stop.json"] == {"force": True}
    assert len(result["failures"]) == 1
    assert set(result["failures"][0]) == {"operation", "result", "failure"}


@pytest.mark.parametrize("content", [None, "invalid JSON", "[]"])
def test_failure_snapshot_does_not_mask_the_original_error(tmp_path, content):
    state = tmp_path / ".artifacts/state"
    state.mkdir(parents=True)
    if content is not None:
        (state / "monitor.json").write_text(content)
    result = failure_snapshot(tmp_path)
    assert result["monitor.json"] and result["stop.json"]
