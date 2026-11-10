"""Check shared WAI conditions and native production stage boundaries."""

from pathlib import Path
from uuid import uuid4

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.wai_guides import prepare_guides
from komaquill.adapters.workflows import assets, prepare
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api.runtime import RuntimeService
from komaquill.application.images.generation import (
    generation_settings,
    output_spec,
    retain_rewritten_scene,
)
from komaquill.application.preparation.service import requirements
from komaquill.domain.assets import active_workflow_nodes, workflow_models
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import compile_image_input
from komaquill.domain.library.models import ImagePromptDefaults, WaiPreferences
from komaquill.domain.presets import WAI_PROFILE
from komaquill.domain.workflow import digest
from PIL import Image


def test_wai_defaults_validate_and_keep_individual_overrides():
    settings = Settings(models=[WAI_PROFILE])
    defaults = WaiPreferences(
        quality_enabled=False,
        quality_positive="custom quality",
        quality_negative="custom exclusions",
        candidates=3,
        cfg=4,
        width=768,
    )
    updated = generation_settings(settings, WAI_PROFILE, 2, {"width": 1152}, wai=defaults)
    selected = updated.presets[WAI_PROFILE]
    applied = compile_image_input(
        selected, "two figures", common_prompt="window light", negative="text"
    )
    assert applied.positive == "window light, two figures"
    assert applied.negative == "text"
    assert selected.negative_default == ""
    assert settings.presets[WAI_PROFILE].negative_default
    api, ui = prepare("two figures", 3, "test", settings=updated, compiled_input=applied)
    nodes = {node["id"]: node for node in ui["nodes"]}
    assert nodes[100]["widgets_values"] == [api["100"]["inputs"]["value"]] == ["two figures"]
    assert nodes[101]["widgets_values"][0] == api["101"]["inputs"]["string_a"] == "window light"
    assert nodes[3]["widgets_values"][3] == api["3"]["inputs"]["cfg"] == 4
    assert nodes[5]["widgets_values"][2] == api["5"]["inputs"]["batch_size"] == 2
    assert nodes[68]["widgets_values"][0] == api["68"]["inputs"]["value"] == 1152


@pytest.mark.parametrize(
    ("value", "quality"),
    [(None, True), ("", False)],
)
def test_quality_field_distinguishes_default_and_empty(value, quality):
    settings = Settings(models=[WAI_PROFILE])
    wai = WaiPreferences(quality_positive=value, quality_negative=value)
    effective = generation_settings(settings, WAI_PROFILE, wai=wai).presets[WAI_PROFILE]
    assert bool(effective.positive_prefix) is quality
    assert bool(effective.negative_default) is quality
    assert effective.quality_enabled


@pytest.mark.parametrize(
    "overrides",
    [
        {"sampler": "unknown"},
        {"scheduler": "unknown"},
        {"cfg": 0.5},
        {"width": 765},
        {"candidates": 5},
    ],
)
def test_saved_overrides_use_preset_validation(overrides):
    with pytest.raises(KomaquillError, match="invalid_request"):
        generation_settings(
            Settings(models=[WAI_PROFILE]), WAI_PROFILE, wai=WaiPreferences(**overrides)
        )


def test_bypassed_workflow_needs_no_production_assets():
    settings = Settings(models=[WAI_PROFILE])
    api, graph = prepare("a cabin", 7, "test", settings=settings, randomize_seed=True)
    stage_nodes = [node for node in graph["nodes"] if node["mode"] == 4]
    assert len(stage_nodes) == 5
    assert set(api) == {key for key, _ in active_workflow_nodes(graph)}
    assert workflow_models(graph) == []
    assert {value.name for value in assets(settings)} == {WAI_PROFILE, "illustrious-detail"}
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes[3]["widgets_values"][:2] == [7, "randomize"]
    definitions = {node["class_type"]: {"input": {}} for node in api.values()}
    for node in (nodes[4], nodes[90]):
        definitions[node["type"]]["input"] = {"required": {"weights": [node["widgets_values"][:1]]}}
    assert requirements(graph, definitions) == ([], [])

    rewrite = stage_nodes[0]
    rewrite["mode"] = 0
    hints, missing = requirements(graph, definitions)
    assert len(hints) == 1
    assert hints[0]["name"] == settings.assets["qwen35-4b-text"].reference
    assert hints[0]["node_id"].startswith(f"{rewrite['id']}:")
    assert "TextGenerate" in missing
    assert "ControlNetLoader" not in missing


def test_default_native_connections_match_the_fixed_api_graph():
    api, graph = prepare("a cabin", 7, "test", settings=Settings(models=[WAI_PROFILE]))
    nodes = {node["id"]: node for node in graph["nodes"]}
    links = {link["id"]: link for link in graph["links"]}
    for identity, item in api.items():
        node = nodes[int(identity)]
        assert item["class_type"] == node["type"]
        for socket in node.get("inputs", []):
            if socket["link"] is None:
                continue
            link = links[socket["link"]]
            assert nodes[link["target_id"]]["inputs"][link["target_slot"]] == socket
            while nodes[link["origin_id"]]["mode"] == 4:
                stage = nodes[link["origin_id"]]
                bypass = stage["inputs"][link["origin_slot"]]
                assert bypass["type"] == link["type"]
                link = links[bypass["link"]]
            assert item["inputs"][socket["name"]] == [str(link["origin_id"]), link["origin_slot"]]


def test_production_stages_preserve_the_candidate_role():
    settings = Settings(models=[WAI_PROFILE])
    api, graph = prepare("a cabin", 7, "test", settings=settings)
    api["5"]["inputs"]["batch_size"] = 4
    stages = {node["type"]: node for node in graph["nodes"] if node["mode"] == 4}
    for definition in graph["definitions"]["subgraphs"]:
        root = stages[definition["id"]]["id"]
        active = {
            **api,
            **{
                f"{root}:{node['id']}": {"class_type": node["type"], "inputs": {}}
                for node in definition["nodes"]
            },
        }
        finish = definition["name"] == "Refine a saved candidate"
        if finish:
            active["9"] = {"class_type": "SaveImage", "inputs": {"images": ["114:1146", 0]}}
        spec = output_spec(settings, active)
        assert spec is not None and spec.primary_node == "9"
        assert spec.candidates == (None if finish else 4)
        active["new-output"] = {"class_type": "SaveImage", "inputs": {}}
        assert output_spec(settings, active) is None


def test_preset_guides_can_be_selected_without_overwriting_inputs(tmp_path: Path):
    prepare_guides(tmp_path)
    files = list((tmp_path / "komaquill-guides").glob("*.png"))
    assert len(files) == 4
    for file in files:
        with Image.open(file) as image:
            assert image.size == (512, 512)
            assert image.getbbox()
    files[0].write_bytes(b"existing guide")
    prepare_guides(tmp_path)
    assert files[0].read_bytes() == b"existing guide"


def test_export_uses_saved_settings_in_both_views(tmp_path: Path):
    settings = Settings(models=[WAI_PROFILE])
    defaults = ImagePromptDefaults(
        positive="cloudy sky",
        negative="text",
        wai=WaiPreferences(quality_positive="", quality_negative="", seed=0, candidates=4),
    )
    runtime = RuntimeService(RuntimeWorkspace(tmp_path, settings), lambda: defaults)
    app = runtime.workflow(WAI_PROFILE, "app")
    graph = runtime.workflow(WAI_PROFILE, "graph")
    assert app["extra"]["linearMode"]
    assert not graph["extra"]["linearMode"]
    assert app["nodes"] == graph["nodes"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes[3]["widgets_values"][:2] == [0, "fixed"]
    assert nodes[5]["widgets_values"][2] == 4
    assert nodes[7]["widgets_values"][0] == "text"
    assert nodes[10]["widgets_values"][0] == ""
    assert nodes[101]["widgets_values"][0] == "cloudy sky"
    assert runtime.wai_defaults()[0].quality_positive


def test_english_auxiliary_output_stays_with_the_same_run(project):
    identity = uuid4()
    preview = "110:1104"
    graph = {
        "110:1103": {"class_type": "TextGenerate", "inputs": {}},
        preview: {"class_type": "PreviewAny", "inputs": {"source": ["110:1103", 0]}},
        "101": {"class_type": "StringConcatenate", "inputs": {"string_b": [preview, 0]}},
    }
    outputs = {preview: {"text": ["Two people share a blue umbrella."]}}
    retain_rewritten_scene(project, identity, graph, outputs)
    retain_rewritten_scene(project, identity, graph, outputs)
    key = f"runs/{identity}/rewritten-scenes/{digest({'node': preview})}.json"
    assert b"Two people share a blue umbrella." in project.artifacts_store.read(key)
    with pytest.raises(KomaquillError, match="invalid_output"):
        retain_rewritten_scene(project, identity, graph, {preview: {"text": ["Different text"]}})
