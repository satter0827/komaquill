"""Verify workflow exports and package structure."""

from pathlib import Path

from komaquill.adapters.workspace import find_project

ROOT = Path(__file__).resolve().parents[4]


def test_subdirectory_uses_the_project_artifacts(monkeypatch):
    monkeypatch.delenv("KOMAQUILL_PROJECT_DIR", raising=False)
    monkeypatch.chdir(ROOT / "src/backend/src/komaquill")
    assert find_project().artifacts == ROOT / ".artifacts"


def test_root_workspace_settings_are_valid():
    """Include the shipped workspace configuration in the startup contract."""
    assert find_project(ROOT).settings().worker_execution_timeout > 0
