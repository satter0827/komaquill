"""Exercise local bundle replacement and frozen workflow snapshots."""

import json
import shutil
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.preset_catalog import bundle
from komaquill.infrastructure.workflows import prepare


def copy_bundle(project, name="klein-4b"):
    target = project.root / "presets" / name
    shutil.copytree(Path(str(files("komaquill").joinpath("resources/presets/klein-4b"))), target)
    if name != "klein-4b":
        manifest = target / "preset.toml"
        manifest.write_text(
            manifest.read_text().replace('model_id = "klein-4b"', f'model_id = "{name}"')
        )
    return target


def test_local_preset_is_complete_and_frozen(project, monkeypatch):
    target = copy_bundle(project, "custom")
    config = project.root / "komaquill.toml"
    config.write_text('models=["custom"]\n[presets.custom]\nsteps=31\n')
    frozen = project.configured()
    first, _ = prepare("first", 42, "run", settings=frozen.settings())
    assert first["62"]["inputs"]["steps"] == 31
    first["62"]["inputs"]["steps"] = 999
    manifest = target / "preset.toml"
    manifest.write_text(manifest.read_text().replace("steps = 4", "steps = 40"))
    config.write_text('models=["custom"]\n')
    second, _ = prepare("second", 42, "run", settings=frozen.settings())
    assert second["62"]["inputs"]["steps"] == 31
    assert second["63"]["inputs"]["conditioning"] == ["74", 0]
    assert project.settings().presets["custom"].steps == 40
    monkeypatch.setenv("KOMAQUILL_PRESETS", json.dumps({"custom": {"steps": 41}}))
    assert project.settings().presets["custom"].steps == 41


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "invalid_name",
        "bad_source",
        "bad_target",
        "bad_widget",
        "duplicate",
        "missing_binding",
    ],
)
def test_invalid_bundles_fail_before_start(project, case):
    target = copy_bundle(project, "BadName" if case == "invalid_name" else "klein-4b")
    manifest = target / "preset.toml"
    text = manifest.read_text()
    if case == "missing":
        (target / "workflow.json").unlink()
    elif case == "bad_source":
        manifest.write_text(text.replace('source = "seed"', 'source = "unknown"'))
    elif case == "bad_target":
        manifest.write_text(text.replace('api_node = "4"', 'api_node = "unknown"', 1))
    elif case == "bad_widget":
        manifest.write_text(text.replace("ui_widget = 0", "ui_widget = 999", 1))
    elif case == "duplicate":
        manifest.write_text(
            text
            + text[
                text.index("[[bindings]]") : text.index(
                    "[[bindings]]", text.index("[[bindings]]") + 1
                )
            ]
        )
    elif case == "missing_binding":
        manifest.write_text(text.replace('source = "seed"', 'source = "steps"'))
    with pytest.raises((KomaquillError, ValidationError)):
        bundle(target)


def test_packaged_graphs_are_independent_copies():
    settings = Settings()
    first, ui = prepare("one", 1, "first", settings=settings)
    first["4"]["inputs"]["unet_name"] = "changed"
    ui["nodes"].clear()
    second, other = prepare("two", 2, "second", settings=settings)
    assert second["4"]["inputs"]["unet_name"] != "changed"
    assert other["nodes"]


@pytest.mark.parametrize("name", ["klein-4b", "klein-4b-edit"])
def test_app_mode_exposes_only_declared_scalar_inputs(name):
    root = files("komaquill").joinpath("resources/presets", name)
    graph = json.loads(root.joinpath("workflow.json").read_bytes())
    nodes = {str(row["id"]): row for row in graph["nodes"]}
    for identity, key in graph["extra"]["linearData"]["inputs"]:
        assert any(slot["name"] == key for slot in nodes[identity]["inputs"])
    assert ["10", "string_b"] in graph["extra"]["linearData"]["inputs"]
    assert ["5", "batch_size"] in graph["extra"]["linearData"]["inputs"]
