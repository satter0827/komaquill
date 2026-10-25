"""Verify that editor entrypoints delegate to the public CLI and quality gates."""

import json
from pathlib import Path

from typer.testing import CliRunner

from komaquill.interfaces.cli import app

ROOT = Path(__file__).resolve().parents[2]


def test_launch_inputs_are_standard_and_resolve_to_valid_cli_commands():
    launch = json.loads((ROOT / ".vscode/launch.json").read_bytes())
    inputs = {entry["id"]: entry for entry in launch["inputs"]}
    assert len(inputs) == len(launch["inputs"])
    defaults = {}
    for identity, entry in inputs.items():
        assert entry["type"] in {"promptString", "pickString"}
        if entry["type"] == "pickString":
            values = [row["value"] if isinstance(row, dict) else row for row in entry["options"]]
            assert entry["default"] in values
            defaults[identity] = entry["default"]
        else:
            defaults[identity] = 'a blue cabin, "watercolor"'
    used = set()
    for entry in launch["configurations"]:
        assert entry["module"] == "komaquill" and "program" not in entry
        assert entry["console"] == "integratedTerminal"
        args = entry["args"]
        if isinstance(args, str):
            assert args == "${command:pickArgs}"
            continue
        resolved = []
        for arg in args:
            if arg.startswith("${input:"):
                identity = arg[8:-1]
                used.add(identity)
                resolved.append(defaults[identity])
            else:
                resolved.append(arg)
        result = CliRunner().invoke(app, [*resolved, "--help"])
        assert result.exit_code == 0, (entry["name"], result.output)
    assert used == inputs.keys()


def test_tasks_keep_process_arguments_and_quality_dependencies_explicit():
    config = json.loads((ROOT / ".vscode/tasks.json").read_bytes())
    tasks = {entry["label"]: entry for entry in config["tasks"]}
    assert len(tasks) == len(config["tasks"])
    for entry in tasks.values():
        if "dependsOn" in entry:
            assert entry["dependsOrder"] == "sequence"
            assert all(label in tasks for label in entry["dependsOn"])
            assert "command" not in entry
        else:
            assert entry["type"] == "process"
            args = entry["args"]
            assert isinstance(args, list)
            targets = [arg for arg in args if arg.endswith((".py", ".ps1"))]
            for target in targets:
                relative = target.removeprefix("${workspaceFolder}/")
                assert (ROOT / relative).is_file()
    visible = [entry for entry in tasks.values() if not entry.get("hide")]
    assert len(visible) == 3
    assert config["options"]["env"]["KOMAQUILL_UV"].startswith(
        "${workspaceFolder}/.artifacts/runtime/"
    )


def test_all_cli_help_is_explicit_and_independent_of_docstrings():
    from typer.main import get_command

    command = get_command(app)
    assert "Generate" in command.help
    for child in command.commands.values():
        assert child.help
        if hasattr(child, "commands"):
            assert all(grandchild.help for grandchild in child.commands.values())
    result = CliRunner().invoke(app, ["generate", "--help"])
    assert result.exit_code == 0
    assert "Generate" in result.output
    assert "Generate an image" not in result.output


def test_malformed_workspace_configuration_has_a_safe_error(project):
    (project.root / "komaquill.toml").write_text("private = [")
    result = CliRunner().invoke(app, ["--project", str(project.root), "start"])
    assert result.exit_code == 1 and "Traceback" not in result.output
    assert "configuration" in result.output


def test_real_story_launches_follow_the_workspace_profile():
    config = json.loads((ROOT / ".vscode/launch.json").read_bytes())
    entries = [
        entry
        for entry in config["configurations"]
        if isinstance(entry["args"], list) and "--allow-real-llm" in entry["args"]
    ]
    assert entries
    assert all("--llm-profile" not in entry["args"] for entry in entries)


def test_image_entry_preserves_preset_defaults_and_file_entry_accepts_both_prompts():
    config = json.loads((ROOT / ".vscode/launch.json").read_bytes())
    entries = {entry["name"]: entry["args"] for entry in config["configurations"]}
    assert "--prompt" in entries["Generate Image"]
    assert "--negative" not in entries["Generate Image"]
    assert "--input-file" in entries["Generate Image from File"]
