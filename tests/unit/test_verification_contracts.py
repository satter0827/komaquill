"""Verify complete check execution and preservation of the primary storage failure."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from komaquill.infrastructure.storage import atomic_bytes
from scripts import check


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        PermissionError(),
        KeyboardInterrupt(),
        subprocess.CalledProcessError(7, ["tool"]),
    ],
)
def test_incomplete_gate_preserves_previous_success(tmp_path, monkeypatch, error):
    monkeypatch.setattr(check, "__file__", str(tmp_path / "scripts/check.py"))
    monkeypatch.setenv("KOMAQUILL_UV", "uv-test")
    monkeypatch.setattr(check, "source_revision", lambda: "fixture")
    pointer = tmp_path / ".artifacts/records/reports/quality/latest-quality.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text('{"report":"previous"}')
    calls = 0

    def execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise error
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    with pytest.raises(type(error)):
        check.main("quality")
    assert json.loads(pointer.read_text()) == {"report": "previous"}
    report = json.loads(next(pointer.parent.glob("*/gate.json")).read_text())
    assert report["status"] != "passed"
    assert report["planned_steps"] > len(report["steps"])
    assert report["steps"][-1]["exit_code"] != 0


def test_gate_input_fingerprint_includes_tests_and_configuration(tmp_path):
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests/test_example.py"
    test.write_text("first")
    initial = check.input_fingerprint(tmp_path)
    test.write_text("second")
    assert check.input_fingerprint(tmp_path) != initial
    baseline = check.input_fingerprint(tmp_path)
    (tmp_path / ".artifacts").mkdir()
    (tmp_path / ".artifacts/generated.json").write_text("ignored")
    assert check.input_fingerprint(tmp_path) == baseline
    (tmp_path / "pyproject.toml").write_text("changed")
    assert check.input_fingerprint(tmp_path) != baseline


def test_atomic_cleanup_does_not_replace_write_failure(tmp_path, monkeypatch, capsys):
    original = OSError("write failure")
    monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(original))
    monkeypatch.setattr(
        Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError())
    )
    with pytest.raises(OSError) as failure:
        atomic_bytes(tmp_path / "record.json", b"{}")
    assert failure.value is original
    assert "temporary file" in capsys.readouterr().err


def test_report_failure_does_not_replace_tool_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "__file__", str(tmp_path / "scripts/check.py"))
    monkeypatch.setenv("KOMAQUILL_UV", "uv-test")
    monkeypatch.setattr(check, "source_revision", lambda: "fixture")
    original = subprocess.CalledProcessError(9, ["tool"])
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(original))
    monkeypatch.setattr(
        check, "atomic_json", lambda *a, **k: (_ for _ in ()).throw(PermissionError())
    )
    with pytest.raises(subprocess.CalledProcessError) as failure:
        check.main("quality")
    assert failure.value is original
    assert "report could not" in capsys.readouterr().err
