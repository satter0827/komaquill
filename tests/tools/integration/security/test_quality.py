"""Verify quality contracts within the declared test scope."""

import json
from types import SimpleNamespace

import pytest

from tools.checks import secrets as security


@pytest.mark.parametrize("case", ["clean", "tracked", "file_secret", "diff_secret"])
def test_security_gate_reports_scope_and_never_secret_values(tmp_path, monkeypatch, case):
    (tmp_path / "example.txt").write_text("public content")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "__file__", str(tmp_path / "tools/checks/secrets.py"))

    def git_text(root, *args):
        if args[:1] == ("rev-parse",):
            return "test-revision"
        if args[:1] == ("diff",) or args[:1] == ("log",):
            return "fake diff" if case == "diff_secret" else ""
        if args == ("ls-files", "-z"):
            return ".artifacts/private.txt" if case == "tracked" else "example.txt\0"
        return "example.txt\0"

    monkeypatch.setattr(security, "git_text", git_text)
    finding = SimpleNamespace(
        filename="example.txt",
        line_number=1,
        type="test",
        secret_value="do-not-copy",  # pragma: allowlist secret Test-only redaction sentinel.
    )
    monkeypatch.setattr(security, "scan_file", lambda _: [finding] if case == "file_secret" else [])
    monkeypatch.setattr(security, "scan_diff", lambda _: [finding])
    if case == "clean":
        security.main()
    else:
        with pytest.raises(SystemExit):
            security.main()
    if case != "tracked":
        report = next(
            (tmp_path / ".artifacts/records/reports/quality").glob("*/security-history.json")
        ).read_text(encoding="utf-8")
        data = json.loads(report)
        assert data["scope"] == ["working_tree", "staged", "history"]
        assert data["source_revision"] == "test-revision"
        assert "do-not-copy" not in report and data["created_at"]


def test_security_git_invocation_preserves_argument_boundaries(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        security.subprocess, "check_output", lambda args, **kwargs: calls.append(args) or "value"
    )
    assert security.git_text(tmp_path, "show", "a b") == "value"
    assert calls == [["git", "-C", str(tmp_path), "show", "a b"]]
