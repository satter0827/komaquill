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


def copy_bundle(project, name="pony"):
    target = project.root / "presets" / name
    shutil.copytree(Path(str(files("komaquill").joinpath("resources/presets/pony"))), target)
    return target


def test_local_preset_is_complete_and_frozen(project, monkeypatch):
    target = copy_bundle(project, "custom")
    config = project.root / "komaquill.toml"
    config.write_text('profiles=["custom"]\n[presets.custom]\nsteps=31\n')
    frozen = project.configured()
    first, _ = prepare("first", None, 42, "run", settings=frozen.settings())
    assert first["3"]["inputs"]["steps"] == 31
    first["3"]["inputs"]["steps"] = 999
    manifest = target / "preset.toml"
    manifest.write_text(manifest.read_text().replace("steps = 25", "steps = 40"))
    config.write_text('profiles=["custom"]\n')
    second, _ = prepare("second", "", 42, "run", settings=frozen.settings())
    assert second["3"]["inputs"]["steps"] == 31
    profile = frozen.settings().presets["custom"]
    negative = next(binding for binding in profile.bindings if binding.source == "negative")
    prefix = next(binding for binding in profile.bindings if binding.source == "negative_prefix")
    assert second[negative.api_node]["inputs"][negative.api_input] == ""
    assert second[prefix.api_node]["inputs"][prefix.api_input] == profile.negative_prefix
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
    target = copy_bundle(project, "BadName" if case == "invalid_name" else "pony")
    manifest = target / "preset.toml"
    text = manifest.read_text()
    if case == "missing":
        (target / "workflow.json").unlink()
    elif case == "bad_source":
        manifest.write_text(text.replace('source = "seed"', 'source = "unknown"'))
    elif case == "bad_target":
        manifest.write_text(text.replace('api_node = "3"', 'api_node = "unknown"', 1))
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
    first, ui = prepare("one", None, 1, "first", settings=settings)
    first["4"]["inputs"]["ckpt_name"] = "changed"
    ui["nodes"].clear()
    second, other = prepare("two", None, 2, "second", settings=settings)
    assert second["4"]["inputs"]["ckpt_name"] != "changed"
    assert other["nodes"]


@pytest.mark.parametrize("name", ["pony", "pony-preview", "pony-story", "pony-upscale", "sd15"])
def test_negative_inputs_remain_visible_without_overlapping_other_nodes(name):
    root = files("komaquill").joinpath("resources/presets", name)
    graph = json.loads(root.joinpath("workflow.json").read_bytes())
    nodes = graph["nodes"]
    negative = [
        row for row in nodes if row.get("title") in {"Negative Prompt", "Negative Conditioning"}
    ]
    assert len(negative) == 2
    for node in negative:
        x, y = node["pos"]
        width, height = node["size"]
        for other in nodes:
            if other["id"] == node["id"]:
                continue
            ox, oy = other["pos"]
            ow, oh = other["size"]
            assert x + width <= ox or ox + ow <= x or y + height + 30 <= oy or oy + oh + 30 <= y
