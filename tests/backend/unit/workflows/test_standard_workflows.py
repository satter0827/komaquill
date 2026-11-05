"""Verify workflow exports and package structure."""

from pathlib import Path

from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import prepare, resource_json

ROOT = Path(__file__).resolve().parents[4]


def test_workflow_exports_have_identical_connections_and_parameters():
    api = resource_json("presets/klein-4b/workflow-api.json")
    ui = resource_json("presets/klein-4b/workflow.json")
    nodes = {str(node["id"]): node for node in ui["nodes"]}
    links = {link[0]: link for link in ui["links"]}
    for key, node in api.items():
        assert nodes[key]["type"] == node["class_type"]
        for socket in nodes[key].get("inputs", []):
            if socket["link"] is None:
                continue
            link = links[socket["link"]]
            assert node["inputs"][socket["name"]] == [str(link[1]), link[2]]
    from komaquill.adapters.configuration import Settings

    profile = Settings().presets["klein-4b"]
    for binding in profile.bindings:
        assert (
            api[binding.api_node]["inputs"][binding.api_input]
            == nodes[binding.api_node]["widgets_values"][binding.ui_widget]
        )
    assert ui["extra"]["linearData"]["outputs"] == ["9"]


def test_prepared_workflow_records_the_same_requested_values():
    api, ui = prepare(
        "a cabin", 123, "test-id", profile_name="klein-4b", settings=Settings(models=["klein-4b"])
    )
    nodes = {node["id"]: node for node in ui["nodes"]}
    assert api["10"]["inputs"]["string_b"] == nodes[10]["widgets_values"][1] == "a cabin"
    assert api["73"]["inputs"]["noise_seed"] == nodes[73]["widgets_values"][0] == 123
    assert api["9"]["inputs"]["filename_prefix"] == nodes[9]["widgets_values"][0]
    assert resource_json("presets/klein-4b/workflow-api.json")["73"]["inputs"]["noise_seed"] == 1
