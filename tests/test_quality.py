"""Test quality gates with real reports and isolated archives."""

import io
import json
import subprocess
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

from scripts import check, check_coverage, check_package, security


def test_thresholds_are_independent_and_not_rounded():
    assert not check_coverage.check_totals(
        {"covered_lines": 90, "num_statements": 100, "covered_branches": 80, "num_branches": 100},
        90,
        80,
    )
    assert check_coverage.check_totals(
        {
            "covered_lines": 89995,
            "num_statements": 100000,
            "covered_branches": 79995,
            "num_branches": 100000,
        },
        90,
        80,
    ) == ["行", "分岐"]


@pytest.mark.parametrize(
    "totals",
    [
        {"covered_lines": 0, "num_statements": 0, "covered_branches": 0, "num_branches": 0},
        {"covered_lines": 101, "num_statements": 100, "covered_branches": 0, "num_branches": 1},
        {"covered_lines": "90", "num_statements": 100, "covered_branches": 1, "num_branches": 1},
    ],
)
def test_invalid_measurements_are_rejected(totals):
    with pytest.raises(ValueError):
        check_coverage.check_totals(totals, 90, 80)


@pytest.mark.parametrize("case", ["pass", "threshold", "inventory"])
def test_coverage_gate_checks_source_inventory_and_configured_policy(tmp_path, monkeypatch, case):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("pass")
    (tmp_path / "scripts/gate.py").write_text("pass")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.komaquill.coverage]\nline_minimum=90\nbranch_minimum=80"
    )
    folder = tmp_path / ".artifacts/reports/quality"
    folder.mkdir(parents=True)
    totals = {
        "covered_lines": 90 if case != "threshold" else 89,
        "num_statements": 100,
        "covered_branches": 80,
        "num_branches": 100,
    }
    files = {"src/app.py": {}, "scripts/gate.py": {}}
    if case == "inventory":
        files["tests/test_app.py"] = {}
    (folder / "coverage.json").write_text(json.dumps({"files": files, "totals": totals}))
    monkeypatch.setattr(check_coverage, "__file__", str(tmp_path / "scripts/check_coverage.py"))
    if case == "pass":
        check_coverage.main()
    else:
        with pytest.raises(SystemExit):
            check_coverage.main()


def test_quality_gate_runs_tools_in_order_and_stops_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "__file__", str(tmp_path / "scripts/check.py"))
    commands = []
    monkeypatch.setattr(
        check.subprocess, "run", lambda command, **kwargs: commands.append((command, kwargs))
    )
    check.main()
    assert commands[0][0][2:4] == ["ruff", "format"]
    assert any("--cov" in args for args, _ in commands)
    assert commands[-1][0][-1].endswith("check_coverage.py")
    assert all(options["check"] and options["cwd"] == tmp_path for _, options in commands)

    def failure(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(check.subprocess, "run", failure)
    with pytest.raises(subprocess.CalledProcessError):
        check.main()


@pytest.mark.parametrize(
    "case",
    [
        "pass",
        "installed",
        "tag",
        "wheel_version",
        "resources",
        "sdist_version",
        "missing_metadata",
        "artifacts",
    ],
)
def test_distribution_gate_checks_real_archives(tmp_path, monkeypatch, case):
    expected = "1.2.3"
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="1.2.3"')
    folder = tmp_path / ".artifacts/dist"
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / f"komaquill-{expected}-py3-none-any.whl", "w") as archive:
        archive.writestr(
            f"komaquill-{expected}.dist-info/METADATA",
            "Version: " + ("0.0.0" if case == "wheel_version" else expected),
        )
        for resource in ("workflow.json", "workflow-api.json", "model.toml", "runtime.toml"):
            if case != "resources":
                archive.writestr("komaquill/resources/" + resource, "data")
        if case == "artifacts":
            archive.writestr(".artifacts/private.txt", "private")
    with tarfile.open(folder / f"komaquill-{expected}.tar.gz", "w:gz") as archive:
        if case != "missing_metadata":
            data = ("Version: " + ("0.0.0" if case == "sdist_version" else expected)).encode()
            member = tarfile.TarInfo(f"komaquill-{expected}/PKG-INFO")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    monkeypatch.setattr(check_package, "__file__", str(tmp_path / "scripts/check_package.py"))
    monkeypatch.setattr(
        check_package, "version", lambda _: "0.0.0" if case == "installed" else expected
    )
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag" if case == "tag" else "branch")
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.0.0")
    if case == "pass":
        check_package.main()
    else:
        with pytest.raises(SystemExit):
            check_package.main()


@pytest.mark.parametrize("case", ["clean", "tracked", "file_secret", "diff_secret"])
def test_security_gate_reports_scope_and_never_secret_values(tmp_path, monkeypatch, case):
    (tmp_path / "example.txt").write_text("public content")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "__file__", str(tmp_path / "scripts/security.py"))

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
        report = (tmp_path / ".artifacts/reports/quality/security.json").read_text(encoding="utf-8")
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
