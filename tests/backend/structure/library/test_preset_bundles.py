"""Verify library contracts within the declared test scope."""

import json
from importlib.resources import files

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import prepare


def test_packaged_graphs_are_independent_copies():
    settings = Settings(models=["klein-4b"])
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
