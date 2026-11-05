"""Verify workflow exports and package structure."""

import subprocess
from pathlib import Path

from komaquill.adapters.workspace import find_project

ROOT = Path(__file__).resolve().parents[4]


def test_generation_paths_agree_across_entrypoints_and_tools():
    import json
    import re
    import tomllib

    assert find_project(ROOT).artifacts == ROOT / ".artifacts"
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["uv"]["cache-dir"] == ".artifacts/cache/uv"
    assert config["tool"]["mypy"]["cache_dir"] == ".artifacts/cache/mypy"
    settings = json.loads((ROOT / ".vscode/settings.json").read_text(encoding="utf-8"))
    assert settings["python.defaultInterpreterPath"].startswith("${workspaceFolder}/.artifacts/")
    launch = json.loads((ROOT / ".vscode/launch.json").read_text(encoding="utf-8"))
    for entry in launch["configurations"]:
        assert entry["module"] == "komaquill"
        assert entry["envFile"] == "${workspaceFolder}/.artifacts/runtime/vscode.env"
    files = [
        "bootstrap/_environment.ps1",
        "bootstrap/setup.ps1",
        "src/backend/src/komaquill/worker/bootstrap.py",
        "tools/checks/run.py",
        ".github/workflows/check.yml",
        ".vscode/tasks.json",
    ]
    for name in files:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert ".artifacts" in text
        assert not re.search(r"(?<![.\w])artifacts[/\\]", text), name


def test_artifacts_are_ignored_without_creating_a_directory():
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", ".artifacts/probe.png"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_test_discovery_and_cache_stay_inside_the_artifact_root():
    import json
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]
    assert config["cache_dir"] == ".artifacts/cache/pytest"
    assert "scripts/acceptance" not in config["testpaths"]
    assert "no:cacheprovider" not in config["addopts"]
    editor = json.loads((ROOT / ".vscode/settings.json").read_bytes())
    assert set(editor["python.testing.pytestArgs"]) <= set(config["testpaths"])
    assert "scripts/acceptance" not in editor["python.testing.pytestArgs"]


def test_cached_project_outputs_cannot_replace_verification_records():
    import json

    defaults = json.loads((ROOT / "nx.json").read_bytes())["targetDefaults"]
    records = (ROOT / ".artifacts/records").resolve()
    projects = [
        *ROOT.glob("*/project.json"),
        *ROOT.glob("src/*/project.json"),
        *ROOT.glob("tests/*/project.json"),
    ]
    for project in projects:
        for name, target in json.loads(project.read_bytes())["targets"].items():
            if not target.get("cache", defaults.get(name, {}).get("cache", False)):
                continue
            for output in target.get("outputs", []):
                path = output.replace("{workspaceRoot}", str(ROOT)).replace(
                    "{projectRoot}", str(project.parent)
                )
                owned = Path(path.split("*", 1)[0]).resolve()
                assert (
                    owned != records
                    and owned not in records.parents
                    and records not in owned.parents
                ), (project, name, output)
