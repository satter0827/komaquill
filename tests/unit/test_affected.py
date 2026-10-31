"""Verify related selection follows dependencies and excludes unrelated features."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import _support, affected
from scripts.affected import changed, related


@pytest.mark.parametrize("base", [None, "", "0" * 40, "0" * 64, "explicit-ref"])
def test_comparison_base_replaces_only_absent_or_null_git_refs(tmp_path, monkeypatch, base):
    (tmp_path / "nx.json").write_text(json.dumps({"defaultBase": "fixture-base"}))
    monkeypatch.setattr(_support, "ROOT", tmp_path)
    if base is None:
        monkeypatch.delenv("NX_BASE", raising=False)
    else:
        monkeypatch.setenv("NX_BASE", base)
    assert _support.comparison_base() == (
        "explicit-ref" if base == "explicit-ref" else "fixture-base"
    )


@pytest.mark.parametrize("all_tests", [False, True])
def test_initial_push_can_select_related_and_all_tests(tmp_path, monkeypatch, capsys, all_tests):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    (tmp_path / "nx.json").write_text(json.dumps({"defaultBase": "fixture-base"}))
    git("init", "--quiet")
    git("add", "nx.json")
    git("commit", "--quiet", "--no-gpg-sign", "-m", "Fixture")
    git("branch", "fixture-base")
    (tmp_path / "new.py").write_text("pass")
    monkeypatch.setattr(_support, "ROOT", tmp_path)
    monkeypatch.setattr(affected, "ROOT", tmp_path)
    monkeypatch.setenv("NX_BASE", "0" * 40)
    monkeypatch.setattr(sys, "argv", ["affected.py", "--plan", *(["--all"] if all_tests else [])])
    affected.main()
    record = json.loads(capsys.readouterr().out)
    assert record["base"] == "fixture-base"
    assert record["changes"] == ["new.py"]
    assert record["selected"] == (list(affected.TEST_ROOTS) if all_tests else [])


def test_changed_leaf_selects_only_dependent_tests(tmp_path):
    files = {
        "src/backend/src/komaquill/conversations/service.py": (
            "from komaquill.domain import facts"
        ),
        "src/backend/src/komaquill/domain/facts.py": "VALUE = 1",
        "src/backend/src/komaquill/images/service.py": "VALUE = 2",
        "tests/unit/test_chat.py": "import komaquill.conversations.service",
        "tests/unit/test_images.py": "from komaquill.images.service import ImageService",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert related(tmp_path, {"src/backend/src/komaquill/domain/facts.py"}) == [
        "tests/unit/test_chat.py"
    ]
    assert related(tmp_path, {"src/frontend/src/features/conversations.tsx"}) == []
    assert len(related(tmp_path, {"pyproject.toml"})) == 2


def test_non_python_inputs_select_their_contract_checks(tmp_path):
    for name in (
        "tests/unit/test_messages.py",
        "tests/integration/test_distribution.py",
        "tests/structure/test_editor.py",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass")
    assert related(tmp_path, {"scripts/messages.json"}) == ["tests/unit/test_messages.py"]
    assert related(tmp_path, {"bootstrap/setup.ps1"}) == ["tests/integration/test_distribution.py"]
    assert related(tmp_path, {".vscode/launch.json"}) == ["tests/structure/test_editor.py"]


def test_relative_imports_dynamic_imports_and_nested_fixtures_are_selected(tmp_path):
    files = {
        "src/backend/src/komaquill/images/__init__.py": "from .service import run",
        "src/backend/src/komaquill/images/service.py": "from . import helper\ndef run(): pass",
        "src/backend/src/komaquill/images/helper.py": "VALUE = 1",
        "tests/unit/test_images.py": "from komaquill.images import run",
        "tests/integration/test_runtime.py": (
            "import importlib\nruntime=importlib.import_module('komaquill.runtime')"
        ),
        "tests/unit/test_other.py": "pass",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert related(tmp_path, {"src/backend/src/komaquill/images/helper.py"}) == [
        "tests/unit/test_images.py"
    ]
    assert related(tmp_path, {"src/backend/src/komaquill/runtime.py"}) == [
        "tests/integration/test_runtime.py"
    ]
    assert related(tmp_path, {"tests/integration/conftest.py"}) == [
        "tests/integration/test_runtime.py"
    ]
    (tmp_path / "src/backend/src/komaquill/images/helper.py").write_text("invalid syntax !")
    assert len(related(tmp_path, {"src/backend/src/komaquill/images/helper.py"})) == 3


def test_base_includes_committed_staged_unstaged_new_and_deleted_changes(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            text=True,
        ).strip()

    git("init", "--quiet")
    for name in ("committed.py", "staged.py", "working.py", "deleted.py"):
        (tmp_path / name).write_text("before")
    git("add", ".")
    git("commit", "--quiet", "--no-gpg-sign", "-m", "Fixture")
    base = git("rev-parse", "HEAD")
    (tmp_path / "committed.py").write_text("after")
    git("add", "committed.py")
    git("commit", "--quiet", "--no-gpg-sign", "-m", "Changed fixture")
    (tmp_path / "staged.py").write_text("after")
    git("add", "staged.py")
    (tmp_path / "working.py").write_text("after")
    (tmp_path / "new file.py").write_text("new")
    (tmp_path / "deleted.py").unlink()
    assert changed(tmp_path, base) == {
        "committed.py",
        "staged.py",
        "working.py",
        "new file.py",
        "deleted.py",
    }


@pytest.mark.parametrize("mode", ["run", "plan", "selection-hash", "missing-base"])
def test_selector_records_scope_and_never_forwards_live_flags(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(affected, "ROOT", tmp_path)
    path = tmp_path / "tests/unit/test_feature.py"
    path.parent.mkdir(parents=True)
    path.write_text("pass")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "affected.py",
            "--base",
            "fixture",
            *(["--" + mode] if mode in {"plan", "selection-hash"} else []),
        ],
    )

    def changes(root, base):
        assert base == "fixture"
        if mode == "missing-base":
            raise subprocess.CalledProcessError(1, ["git"])
        return {"tests/unit/test_feature.py"}

    monkeypatch.setattr(affected, "changed", changes)
    monkeypatch.setenv("KOMAQUILL_LIVE", "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "not-forwarded")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")

    def run(command, **kwargs):
        assert not any(key.startswith(("KOMAQUILL_LIVE", "LANGSMITH_")) for key in kwargs["env"])
        assert "PYTEST_ADDOPTS" not in kwargs["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(affected.subprocess, "run", run)
    if mode in {"run", "missing-base"}:
        with pytest.raises(SystemExit) as result:
            affected.main()
        assert (result.value.code == 0) == (mode == "run")
        record = json.loads(
            next((tmp_path / ".artifacts/records/reports/affected").glob("*.json")).read_text()
        )
        assert record["base"] == "fixture"
        assert record["status"] == ("passed" if mode == "run" else "failed")
    else:
        affected.main()
        assert not (tmp_path / ".artifacts").exists()
