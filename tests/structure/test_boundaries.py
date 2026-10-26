"""Verify workflow exports and package structure."""

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.workflows import prepare, resource_json

ROOT = Path(__file__).resolve().parents[2]


def test_workflow_exports_have_identical_connections_and_parameters():
    api = resource_json("presets/klein/workflow-api.json")
    ui = resource_json("presets/klein/workflow.json")
    nodes = {str(node["id"]): node for node in ui["nodes"]}
    links = {link[0]: link for link in ui["links"]}
    for key, node in api.items():
        assert nodes[key]["type"] == node["class_type"]
        for socket in nodes[key].get("inputs", []):
            if socket["link"] is None:
                continue
            link = links[socket["link"]]
            assert node["inputs"][socket["name"]] == [str(link[1]), link[2]]
    from komaquill.infrastructure.configuration import Settings

    profile = Settings().presets["klein"]
    for binding in profile.bindings:
        assert (
            api[binding.api_node]["inputs"][binding.api_input]
            == nodes[binding.api_node]["widgets_values"][binding.ui_widget]
        )
    assert ui["extra"]["linearData"]["outputs"] == ["9"]


def test_prepared_workflow_records_the_same_requested_values():
    api, ui = prepare("a cabin", None, 123, "test-id")
    nodes = {node["id"]: node for node in ui["nodes"]}
    assert api["10"]["inputs"]["string_b"] == nodes[10]["widgets_values"][1] == "a cabin"
    assert api["73"]["inputs"]["noise_seed"] == nodes[73]["widgets_values"][0] == 123
    assert api["9"]["inputs"]["filename_prefix"] == nodes[9]["widgets_values"][0]
    assert resource_json("presets/klein/workflow-api.json")["73"]["inputs"]["noise_seed"] == 1


def test_import_has_no_filesystem_side_effects(tmp_path):
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(ROOT / ".artifacts/cache/pycache"),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import komaquill.interfaces.cli, komaquill.gpu.worker, komaquill.infrastructure.colab",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    assert not list(tmp_path.iterdir())


def test_module_entrypoint_and_metadata_agree():
    result = subprocess.run(
        [sys.executable, "-m", "komaquill", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == version("komaquill")


def test_subdirectory_uses_the_project_artifacts(monkeypatch):
    monkeypatch.delenv("KOMAQUILL_PROJECT_DIR", raising=False)
    monkeypatch.chdir(ROOT / "src/komaquill")
    assert find_project().artifacts == ROOT / ".artifacts"


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
        "src/komaquill/gpu/bootstrap.py",
        "scripts/check.py",
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


def test_root_workspace_settings_are_valid():
    """Include the shipped workspace configuration in the startup contract."""
    assert find_project(ROOT).settings().worker_execution_timeout > 0


def test_browser_modules_use_comfyuis_uncached_javascript_routes():
    import re
    from importlib.resources import files

    web = files("komaquill").joinpath("interfaces/comfyui/web")
    modules = list(web.iterdir())
    assert modules and all(module.name.endswith(".js") for module in modules)
    for module in modules:
        for relative in re.findall(r'from "(\./[^"]+)"', module.read_text(encoding="utf-8")):
            assert web.joinpath(relative.removeprefix("./")).is_file()


def test_test_discovery_and_cache_stay_inside_the_artifact_root():
    import json
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]
    assert config["cache_dir"] == ".artifacts/cache/pytest"
    assert "tests/live" not in config["testpaths"]
    assert "no:cacheprovider" not in config["addopts"]
    editor = json.loads((ROOT / ".vscode/settings.json").read_bytes())
    assert set(editor["python.testing.pytestArgs"]) <= set(config["testpaths"])
    assert "tests/live" not in editor["python.testing.pytestArgs"]
