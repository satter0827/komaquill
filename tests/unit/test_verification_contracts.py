"""Verify complete check execution and preservation of the primary storage failure."""

from pathlib import Path

import pytest
from komaquill.adapters.atomic import atomic_bytes

from scripts import check


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
