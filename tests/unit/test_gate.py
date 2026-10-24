"""Verify cumulative checks, partial failures and evidence freshness."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import check
from scripts._plan import Check, inventory


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "__file__", str(tmp_path / "scripts/check.py"))
    monkeypatch.setattr(check, "source_revision", lambda *args: "fixture")
    monkeypatch.setattr(check.subprocess, "run", lambda *args, **kwargs: None)
    return tmp_path / ".artifacts/records/reports/quality"


def test_levels_are_cumulative_and_plan_is_read_only(gate, capsys):
    ids = []
    for level in (1, 2, 3):
        assert check.main(level, plan=True, json_output=True)
        data = json.loads(capsys.readouterr().out)
        ids.append({step["id"] for step in data["checks"]})
    assert ids[0] < ids[1] < ids[2]
    assert {"build", "installed-wheel", "secrets-history"} <= ids[2] - ids[1]
    assert not gate.exists()


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        PermissionError(),
        subprocess.TimeoutExpired("tool", 1),
        subprocess.CalledProcessError(7, ["tool"]),
        KeyboardInterrupt(),
    ],
)
def test_failure_does_not_promote_success_and_keeps_independent_checks(gate, monkeypatch, error):
    gate.mkdir(parents=True)
    pointer = gate / "latest-success-l3.json"
    pointer.write_text('{"report":"previous"}')
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if "build" in command:
            raise error

    monkeypatch.setattr(check.subprocess, "run", execute)
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            check.main(3)
    else:
        assert not check.main(3)
    assert json.loads(pointer.read_text()) == {"report": "previous"}
    report = json.loads(next(gate.glob("*/gate.json")).read_text())
    steps = {row["id"]: row for row in report["steps"]}
    assert steps["build"]["status"] in {"failed", "interrupted"}
    assert steps["distribution"]["status"] == "skipped"
    assert len(report["steps"]) == report["planned_steps"]
    if not isinstance(error, KeyboardInterrupt):
        assert steps["secrets-history"]["status"] == "passed"


def test_input_change_prevents_success(gate, monkeypatch):
    fingerprint = iter(["before", "after"])
    monkeypatch.setattr(check, "input_fingerprint", lambda root: next(fingerprint))
    assert not check.main(1)
    assert not (gate / "latest-success-l1.json").exists()


def test_selected_check_never_updates_level_success(gate):
    assert check.main(3, selected=["distribution"])
    report = json.loads(next(gate.glob("*/gate.json")).read_text())
    assert [row["id"] for row in report["steps"]] == ["build", "distribution"]
    assert report["scope"] == "checks" and report["level"] is None
    assert not list(gate.glob("latest-*.json"))


def test_missing_report_storage_is_a_failure(gate, monkeypatch, capsys):
    monkeypatch.setattr(
        check, "atomic_json", lambda *args: (_ for _ in ()).throw(PermissionError())
    )
    assert not check.main(1)
    assert "report could not" in capsys.readouterr().err


@pytest.mark.parametrize("level, selected", [(0, None), (4, None), (2, ["missing"])])
def test_invalid_scope_is_rejected(gate, level, selected):
    with pytest.raises(ValueError):
        check.main(level, selected=selected)


def test_plan_lists_exact_dependency_boundaries():
    checks = inventory(Path("/workspace"), Path("/reports/run"), "uv")
    assert len({item.id for item in checks}) == len(checks)
    assert next(item for item in checks if item.id == "installed-wheel").requires == (
        "distribution",
    )
    assert all(isinstance(item, Check) for item in checks)


def test_no_check_uses_provider_accounts(gate, capsys):
    check.main(3, plan=True, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert all("colab_entry" not in str(row["command"]) for row in data["checks"])


def test_status_is_read_only_and_distinguishes_missing_stale_and_current(gate, capsys):
    check.main(1, status=True, json_output=True)
    assert json.loads(capsys.readouterr().out)["success"] is None
    assert not gate.exists()
    assert check.main(1)
    capsys.readouterr()
    check.main(1, status=True, json_output=True)
    assert json.loads(capsys.readouterr().out)["success"]["matches_current_inputs"]
    root = gate.parents[3]
    (root / "README.md").write_text("changed")
    check.main(1, status=True, json_output=True)
    assert not json.loads(capsys.readouterr().out)["success"]["matches_current_inputs"]


def test_gate_disables_external_tracing_and_removes_inherited_credentials(gate, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_TEST_TRACKING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-only")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "test-only")
    environments = []
    monkeypatch.setattr(
        check.subprocess, "run", lambda *args, **kwargs: environments.append(kwargs["env"])
    )
    assert check.main(1)
    assert environments
    for env in environments:
        assert env["LANGSMITH_TRACING"] == "false"
        assert env["LANGCHAIN_TRACING_V2"] == "false"
        assert env["LANGSMITH_TEST_TRACKING"] == "false"
        assert "LANGSMITH_API_KEY" not in env and "LANGCHAIN_API_KEY" not in env
