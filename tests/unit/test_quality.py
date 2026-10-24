"""Test quality gates with real reports and isolated archives."""

import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check, check_coverage, check_package, check_worker_lock, security


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
    ) == ["Statement", "Branch"]


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
    folder = tmp_path / ".artifacts/records/reports/quality"
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
        check.subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append((command, kwargs)) or SimpleNamespace(returncode=0)
        ),
    )
    monkeypatch.setenv("KOMAQUILL_UV", "uv-test")
    monkeypatch.setattr(check, "source_revision", lambda: None)
    check.main()
    assert commands[0][0][2:4] == ["ruff", "format"]
    assert any("--cov" in args for args, _ in commands)
    assert any("check_coverage.py" in " ".join(args) for args, _ in commands)
    assert any(arg.endswith("security.py") for arg in commands[-1][0])
    assert any("check_e2e.py" in " ".join(args) for args, _ in commands)
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
        "editor_settings",
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
        if case == "editor_settings":
            archive.writestr(".vscode/settings.json", "{}")
        from komaquill.infrastructure.provenance import RESOURCE_NAMES

        for resource in RESOURCE_NAMES:
            if case != "resources":
                archive.writestr("komaquill/resources/" + resource, "data")
        archive.writestr("komaquill/gpu/bootstrap.py", "pass")
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
        report = next(
            (tmp_path / ".artifacts/records/reports/quality").glob("*/security.json")
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


@pytest.mark.parametrize("case", ["matching", "different", "missing_uv"])
def test_gpu_lock_gate_detects_drift_without_resolving_dependencies(tmp_path, monkeypatch, case):
    resource = tmp_path / "src/komaquill/resources/pylock.worker.toml"
    resource.parent.mkdir(parents=True)
    resource.write_text("locked\n")
    monkeypatch.setattr(
        check_worker_lock, "__file__", str(tmp_path / "scripts/check_worker_lock.py")
    )
    if case == "missing_uv":
        monkeypatch.delenv("KOMAQUILL_UV", raising=False)
        monkeypatch.setattr(check_worker_lock.shutil, "which", lambda _: None)
    else:
        monkeypatch.setenv("KOMAQUILL_UV", "uv-test")

    def export(args, **kwargs):
        assert "--locked" in args and kwargs["check"]
        Path(args[args.index("--output-file") + 1]).write_text(
            "different\n" if case == "different" else "locked\n"
        )

    monkeypatch.setattr(check_worker_lock.subprocess, "run", export)
    if case == "matching":
        check_worker_lock.main()
    else:
        with pytest.raises(SystemExit):
            check_worker_lock.main()


def test_worker_lock_writer_documents_public_hash_exceptions(tmp_path, monkeypatch):
    resource = tmp_path / "src/komaquill/resources/pylock.worker.toml"
    resource.parent.mkdir(parents=True)
    monkeypatch.setattr(
        check_worker_lock, "__file__", str(tmp_path / "scripts/check_worker_lock.py")
    )
    monkeypatch.setenv("KOMAQUILL_UV", "uv-test")

    def export(args, **kwargs):
        Path(args[args.index("--output-file") + 1]).write_text(
            'wheels = [{hashes = { sha256 = "public" }}]\n'
        )

    monkeypatch.setattr(check_worker_lock.subprocess, "run", export)
    check_worker_lock.main(write=True)
    text = resource.read_text()
    assert text.endswith("# pragma: allowlist secret Public artifact SHA256 from uv.lock.\n")
    check_worker_lock.main()


@pytest.mark.parametrize("valid", [False, True])
def test_e2e_gate_requires_one_wheel_and_clears_live_flags(tmp_path, monkeypatch, valid):
    from scripts import check_e2e

    wheel = tmp_path / "dist"
    wheel.mkdir()
    if valid:
        (wheel / "komaquill-1.0-py3-none-any.whl").write_bytes(b"fixture")
    monkeypatch.setenv("KOMAQUILL_LIVE", "1")
    monkeypatch.setenv("KOMAQUILL_LIVE_STOP", "normal")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    calls = []
    monkeypatch.setattr(check_e2e.subprocess, "run", lambda command, **kw: calls.append(kw))
    if valid:
        check_e2e.main(wheel, tmp_path / "reports")
        assert calls[0]["env"]["KOMAQUILL_WHEEL"] == str(wheel)
        assert "KOMAQUILL_LIVE" not in calls[0]["env"]
        assert "KOMAQUILL_LIVE_STOP" not in calls[0]["env"]
        assert "PYTEST_ADDOPTS" not in calls[0]["env"]
    else:
        with pytest.raises(SystemExit):
            check_e2e.main(wheel, tmp_path / "reports")


def test_security_group_never_needs_uv_and_missing_uv_fails_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "__file__", str(tmp_path / "scripts/check.py"))
    monkeypatch.delenv("KOMAQUILL_UV", raising=False)
    monkeypatch.setattr(check.shutil, "which", lambda _: None)
    calls = []
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0),
    )
    check.main("security")
    assert len(calls) == 1 and any(arg.endswith("security.py") for arg in calls[0])
    with pytest.raises(SystemExit):
        check.main("quality")
