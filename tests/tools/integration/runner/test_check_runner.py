"""Verify runner contracts within the declared test scope."""

import json
from types import SimpleNamespace

import pytest

from tools.checks import run as check


@pytest.mark.parametrize("scope", ["daily", "ci", "all"])
def test_check_plan_preserves_scope_without_running_tasks(tmp_path, monkeypatch, scope):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )
    assert check.main(scope, plan=True)
    assert ("--affected" in calls[0]) == (scope != "all")
    assert not (tmp_path / ".artifacts").exists()


@pytest.mark.parametrize(
    "case", ["success", "failed", "missing", "interrupted", "changed", "unexpected"]
)
def test_check_records_failure_and_never_promotes_changed_inputs(tmp_path, monkeypatch, case):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check, "source_revision", lambda _: "fixture")
    values = iter(["before", "after" if case == "changed" else "before"])
    monkeypatch.setattr(check, "input_fingerprint", lambda _: next(values))
    monkeypatch.setenv("KOMAQUILL_ALLOW_REAL_LLM", "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "not-forwarded")

    def run(command, **kwargs):
        assert "KOMAQUILL_ALLOW_REAL_LLM" not in kwargs["env"]
        assert "LANGSMITH_API_KEY" not in kwargs["env"]
        if case == "missing":
            raise FileNotFoundError
        if case == "interrupted":
            raise KeyboardInterrupt
        if case == "unexpected":
            raise Exception("private process payload")
        return 1 if case == "failed" else 0

    monkeypatch.setattr(check, "run_owned", run)
    assert check.main("daily") is (case == "success")
    report = json.loads(next((tmp_path / ".artifacts").rglob("checks.json")).read_text())
    assert report["scope"] == "daily"
    assert report["status"] == (
        "passed"
        if case == "success"
        else "interrupted"
        if case in {"missing", "interrupted"}
        else "failed"
    )
    assert report["finished_at"]
    assert "private process payload" not in json.dumps(report)
