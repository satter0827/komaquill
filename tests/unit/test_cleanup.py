"""Verify cleanup retention and deletion against isolated workspace directories."""

import pytest
from filelock import FileLock
from typer.testing import CliRunner

from komaquill.application.cleanup import exclusion
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import cleanup
from komaquill.infrastructure.storage import atomic_json
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
