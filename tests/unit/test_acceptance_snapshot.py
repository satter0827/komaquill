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
    assert set(result["failures"][0]) == {
        "created_at",
        "operation_id",
        "runtime_id",
        "operation",
        "result",
        "failure",
    }


@pytest.mark.parametrize("content", [None, "invalid JSON", "[]"])
def test_failure_snapshot_does_not_mask_the_original_error(tmp_path, content):
    state = tmp_path / ".artifacts/state"
    state.mkdir(parents=True)
    if content is not None:
        (state / "monitor.json").write_text(content)
    result = failure_snapshot(tmp_path)
    assert result["monitor.json"] and result["stop.json"]


def test_first_failure_survives_recent_event_window(tmp_path):
    logs = tmp_path / ".artifacts/records/logs"
    logs.mkdir(parents=True)
    (logs / "events.jsonl").write_text(
        "\n".join(json.dumps({"failure": {"code": str(i)}}) for i in range(20))
    )
    result = failure_snapshot(tmp_path)
    assert result["first_failure"]["failure"]["code"] == "0"
    assert len(result["failures"]) == 12


def test_any_acceptance_failure_is_saved_before_cleanup(tmp_path, monkeypatch):
    from tests.support.acceptance import acceptance_workspace

    report = tmp_path / "evidence"
    monkeypatch.setenv("KOMAQUILL_ACCEPTANCE_REPORT_DIR", str(report))
    with (
        pytest.raises(RuntimeError, match="primary") as caught,
        acceptance_workspace(tmp_path) as root,
    ):
        assert root.exists()
        raise RuntimeError("primary")
    assert not root.exists()
    payload = json.loads(next(report.glob("*.json")).read_bytes())
    assert payload["exception_type"] == "RuntimeError"
    assert "primary" not in repr(payload)
    assert caught.value.__notes__


def test_failed_evidence_write_keeps_primary_error_and_removes_temporary_files(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from tests.support.acceptance import acceptance_workspace

    report = tmp_path / "evidence"
    monkeypatch.setenv("KOMAQUILL_ACCEPTANCE_REPORT_DIR", str(report))
    original = Path.write_text

    def denied(path, *args, **kwargs):
        if path.is_relative_to(report):
            raise PermissionError("private diagnostic")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", denied)
    with (
        pytest.raises(RuntimeError, match="primary") as caught,
        acceptance_workspace(tmp_path) as root,
    ):
        raise RuntimeError("primary")
    assert not root.exists()
    assert "PermissionError" in caught.value.__notes__[0]
    assert "private diagnostic" not in repr(caught.value.__notes__)
