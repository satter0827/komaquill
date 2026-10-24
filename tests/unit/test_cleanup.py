"""Verify cleanup retention and deletion against isolated workspace directories."""

import pytest
from filelock import FileLock
from typer.testing import CliRunner

from komaquill.application.cleanup import exclusion
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import cleanup
from komaquill.infrastructure.atomic import atomic_json
from komaquill.interfaces.cli import app


@pytest.mark.parametrize(
    "category,name,protected,known,busy,reason",
    [
        ("dist", "a" * 32, set(), True, False, None),
        ("dist", "a" * 32, {"a" * 32}, True, False, "retained"),
        ("dist", "unknown", set(), False, False, "unknown"),
        ("dist", "b" * 32, set(), False, False, "unknown"),
        ("cache", "mypy", set(), False, True, "in_use"),
        ("cache", "uv", set(), False, True, None),
        ("cache", "unknown", set(), False, False, "unknown"),
        ("tmp", "pytest-1", set(), False, False, None),
        ("tmp", "pytest-1", set(), False, True, "in_use"),
        ("tmp", "editor.log", set(), False, False, "unknown"),
        ("data", "runs", set(), False, False, "protected"),
    ],
)
def test_retention_policy(category, name, protected, known, busy, reason):
    assert exclusion(category, name, protected, known, busy) == reason


def seed(project):
    for path in (
        project.cache / "ruff/file",
        project.temporary / "pytest-old/file",
        project.data / "runs/work",
        project.records / "logs/proof",
        project.runtime / "venv/keep",
        project.distributions / ("a" * 32) / "old.whl",
        project.distributions / ("b" * 32) / "keep.whl",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained bytes")
    for identity in ("a" * 32, "b" * 32):
        atomic_json(
            project.reports / "quality" / identity / "gate.json", {"group": "quality", "steps": []}
        )
    atomic_json(project.reports / "quality/latest-all.json", {"report": "b" * 32})


def test_preview_then_cleanup_preserves_data_and_latest_distribution(project, monkeypatch):
    seed(project)
    monkeypatch.setattr(cleanup, "cache_in_use", lambda _: False)
    items, report, failed = cleanup.execute(project, True)
    assert report is None and not failed and len(items) == 4
    assert (project.cache / "ruff/file").is_file()
    items, report, failed = cleanup.execute(project, False)
    assert not failed and report.is_file()
    assert not (project.cache / "ruff").exists()
    assert not (project.temporary / "pytest-old").exists()
    assert not (project.distributions / ("a" * 32)).exists()
    for path in (
        project.data / "runs/work",
        project.records / "logs/proof",
        project.runtime / "venv/keep",
        project.distributions / ("b" * 32) / "keep.whl",
    ):
        assert path.read_bytes() == b"retained bytes"


@pytest.mark.parametrize(
    "case", ["owner", "migration", "lock", "failed_delete", "uv", "uv_missing", "link"]
)
def test_cleanup_rejects_unsafe_and_failed_operations(project, monkeypatch, case):
    seed(project)
    monkeypatch.setattr(cleanup, "cache_in_use", lambda _: False)
    if case == "owner":
        atomic_json(project.state / "colab.json", {})
    if case == "migration":
        atomic_json(project.state / "storage-migration.json", {"status": "verifying"})
    if case in {"owner", "migration"}:
        with pytest.raises(KomaquillError):
            cleanup.execute(project, False)
        assert (project.cache / "ruff/file").exists()
        return
    if case == "lock":
        project.state.mkdir(parents=True, exist_ok=True)
        with FileLock(str(project.state / ".cleanup.lock")), pytest.raises(KomaquillError):
            cleanup.execute(project, False)
        return
    if case == "failed_delete":
        monkeypatch.setattr(
            cleanup.shutil, "rmtree", lambda _: (_ for _ in ()).throw(PermissionError())
        )
    elif case.startswith("uv"):
        (project.cache / "uv").mkdir()
        monkeypatch.setattr(cleanup.shutil, "which", lambda _: None)
        monkeypatch.delenv("KOMAQUILL_UV", raising=False)
        if case == "uv":
            monkeypatch.setenv("KOMAQUILL_UV", "uv-test")
            calls = []
            monkeypatch.setattr(cleanup.subprocess, "run", lambda args, **kw: calls.append(args))
    else:
        original = cleanup.measured
        monkeypatch.setattr(
            cleanup, "measured", lambda p: (1, 1, True) if p.name == "ruff" else original(p)
        )
    items, report, failed = cleanup.execute(project, False)
    assert failed is (case in {"failed_delete", "uv_missing"})
    assert report.is_file()
    if case == "uv":
        assert calls[0][1:3] == ["cache", "prune"] and "--force" not in calls[0]
    if case == "link":
        assert (project.cache / "ruff/file").exists()


def test_cleanup_cli_and_busy_cache(project, monkeypatch):
    seed(project)
    monkeypatch.setattr(cleanup, "cache_in_use", lambda _: True)
    result = CliRunner().invoke(app, ["--project", str(project.root), "clean", "--dry-run"])
    assert result.exit_code == 0 and "in use" in result.output
    (project.distributions / "unknown").mkdir()
    items = cleanup.plan(project)
    assert any(row.reason == "unknown" for row in items)
    assert cleanup.measured(project.cache / "ruff/file") == (1, 14, False)


@pytest.mark.parametrize("source", ["environment", "bundled", "path"])
def test_cleanup_locates_uv_without_shell_activation(project, monkeypatch, source):
    import json
    import os

    (project.cache / "uv").mkdir(parents=True)
    monkeypatch.delenv("KOMAQUILL_UV", raising=False)
    monkeypatch.setattr(cleanup.shutil, "which", lambda _: "path-uv")
    bundled = project.runtime / "uv" / ("uv.exe" if os.name == "nt" else "uv")
    if source != "path":
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"test executable")
    if source == "environment":
        monkeypatch.setenv("KOMAQUILL_UV", "configured-uv")
    monkeypatch.setattr(cleanup, "source_revision", lambda: None)
    calls = []
    monkeypatch.setattr(cleanup.subprocess, "run", lambda args, **kw: calls.append(args))
    items, report, failed = cleanup.execute(project, False)
    assert not failed
    assert calls[0][0] == (
        "configured-uv"
        if source == "environment"
        else str(bundled)
        if source == "bundled"
        else "path-uv"
    )
    assert json.loads(report.read_bytes())["status"] == "completed"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("executable", True),
        ("symlink_argv", True),
        ("script", True),
        ("cwd", True),
        ("open_cache", True),
        ("open_tmp", True),
        ("path_environment", True),
        ("virtual_environment", True),
        ("pythonpath_environment", True),
        ("workspace_environment", True),
        ("denied_environment", True),
        ("unrelated", False),
        ("prefix_collision", False),
        ("disappeared", False),
        ("denied_python", True),
        ("denied_other", False),
        ("self", False),
    ],
)
def test_cleanup_detects_workspace_use_beyond_resolved_executable(
    project, monkeypatch, case, expected
):
    import os
    from types import SimpleNamespace

    import psutil

    outside = project.root.parent / "unrelated"
    executable = outside / "python3"
    arguments = [str(executable), "-c", "pass"]
    directory = outside
    files = []
    environment = {}
    if case == "executable":
        executable = project.runtime / "venv/bin/python"
    elif case == "symlink_argv":
        arguments[0] = str(project.runtime / "venv/bin/python")
    elif case == "script":
        arguments = [str(executable), str(project.root / "scripts/check.py")]
    elif case == "cwd":
        directory = project.root
    elif case in {"open_cache", "open_tmp"}:
        folder = project.cache if case == "open_cache" else project.temporary
        files = [SimpleNamespace(path=str(folder / "used/file"))]
    elif case == "prefix_collision":
        arguments[0] = str(
            project.root.with_name(project.root.name + "-other") / ".artifacts/runtime/bin/python"
        )
    if case == "path_environment":
        environment["PATH"] = str(project.runtime / "venv/bin") + os.pathsep + str(outside)
        arguments[0] = "python"
    elif case == "virtual_environment":
        environment["VIRTUAL_ENV"] = str(project.runtime / "venv")
    elif case == "pythonpath_environment":
        environment["PYTHONPATH"] = str(project.root / "src")
    elif case == "workspace_environment":
        environment["KOMAQUILL_PROJECT_DIR"] = str(project.root)
    process = SimpleNamespace(
        pid=os.getpid() if case == "self" else os.getpid() + 1,
        info={"name": "node" if case == "denied_other" else "python3"},
        exe=lambda: str(executable),
        cmdline=lambda: arguments,
        cwd=lambda: str(directory),
        open_files=lambda: files,
        environ=lambda: environment,
    )
    if case.startswith("denied") or case == "disappeared":

        def inaccessible():
            if case == "disappeared":
                raise psutil.NoSuchProcess(process.pid)
            raise psutil.AccessDenied(process.pid)

        if case == "denied_environment":
            process.environ = inaccessible
        else:
            process.exe = inaccessible
    monkeypatch.setattr(cleanup.psutil, "process_iter", lambda _: iter([process]))
    assert cleanup.cache_in_use(project) is expected
