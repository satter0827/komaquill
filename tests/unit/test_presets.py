"""Validate editable standard graphs and shared profile parameters."""

import json

import pytest
from pydantic import ValidationError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.preset_catalog import catalog
from komaquill.infrastructure.workflows import assets, prepare, resource_json, select


@pytest.mark.parametrize("name", sorted(catalog()["presets"]))
def test_profile_exports_match_api_and_widget_values(name):
    settings = Settings(profiles=[name])
    graph, ui = prepare("a blue lake", None, 42, "request", settings=settings)
    profile = settings.presets[name]
    nodes = {str(n["id"]): n for n in ui["nodes"]}
    links = {row[0]: row for row in ui["links"]}
    for key, data in graph.items():
        assert data["class_type"] == nodes[key]["type"]
        for socket in nodes[key].get("inputs", []):
            if socket["link"] is None:
                continue
            link = links[socket["link"]]
            assert data["inputs"][socket["name"]] == [str(link[1]), link[2]]
            assert link[4] == nodes[key]["inputs"].index(socket)
            assert link[0] in nodes[str(link[1])]["outputs"][link[2]]["links"]

    def value(source):
        binding = next(row for row in profile.bindings if row.source == source)
        return graph[binding.api_node]["inputs"][binding.api_input]

    for binding in profile.bindings:
        assert (
            graph[binding.api_node]["inputs"][binding.api_input]
            == nodes[str(binding.ui_node)]["widgets_values"][binding.ui_widget]
        )
    assert value("steps") == profile.steps
    assert value("width") == profile.width
    assert value("height") == profile.height
    assert value("checkpoint") == settings.assets[profile.checkpoint].reference
    assert value("seed") == 42
    assert value("negative") == profile.negative
    assert value("negative_prefix") == profile.negative_prefix
    assert value("batch_size") == profile.batch_size
    custom, _ = prepare("other", "custom", 9, "b", settings=settings)
    negative_binding = next(row for row in profile.bindings if row.source == "negative")
    assert custom[negative_binding.api_node]["inputs"][negative_binding.api_input] == "custom"
    assert ui["extra"]["linearData"]["inputs"]
    assert ui["extra"]["linearData"]["outputs"]


def test_selected_profiles_deduplicate_shared_weights():
    settings = Settings(profiles=["pony", "pony-preview", "pony-upscale"])
    assert [a.name for a in assets(settings)] == ["pony", "hyper-sdxl", "realesrgan-anime"]
    with pytest.raises(KomaquillError):
        select(Settings(), "pony-preview")
    with pytest.raises(KomaquillError):
        resource_json("../workflow.json")


@pytest.mark.parametrize(
    "overrides",
    [
        {"profiles": []},
        {"profiles": ["pony", "pony"]},
        {"profiles": ["unknown"]},
        {"presets": {"pony": {"checkpoint": "missing"}}},
        {"presets": {"pony": {"lora": "pony"}}},
        {"presets": {"bad/name": {}}},
    ],
)
def test_invalid_profile_selection_fails_before_allocation(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_workspace_profile_overrides_and_frozen_snapshot(project, monkeypatch):
    (project.root / "komaquill.toml").write_text(
        'profiles=["pony-preview"]\n[presets.pony-preview]\nsteps=9\n'
    )
    bound = project.configured()
    assert bound.settings().presets["pony-preview"].steps == 9
    monkeypatch.setenv("KOMAQUILL_PROFILES", '["pony"]')
    assert project.settings().profiles == ("pony",)
    assert bound.settings().profiles == ("pony-preview",)
    assert json.loads(bound.settings().model_dump_json())["assets"]["pony"]["sha256"]


@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "../a"},
        {"filename": ".."},
        {"filename": "C:outside"},
        {"filename": "a\\b"},
        {"url": "http://example.com/a"},
        {"repo_id": "", "revision": ""},
        {"url": "https://example.com/a"},
    ],
)
def test_unsafe_or_ambiguous_asset_sources_are_rejected(changes):
    model = Settings().assets["pony"].model_dump()
    with pytest.raises(ValidationError):
        ModelPreset.model_validate({**model, **changes})


@pytest.mark.parametrize("inputs", [None, [], "invalid", {"ckpt_name": "unknown.safetensors"}])
def test_graph_provenance_handles_unknown_models_and_malformed_inputs(project, session, inputs):
    from uuid import uuid4

    from komaquill.application.generation import new_run

    session.settings = project.settings()
    run = new_run(project, session, uuid4(), {"4": {"inputs": inputs}})
    assert run.model_sha256 is None and not run.asset_sha256


def test_graph_provenance_uses_actual_prepared_assets(project, session):
    from uuid import uuid4

    from komaquill.application.generation import new_run

    session.settings = project.settings()
    graph = {"4": {"inputs": {"ckpt_name": session.settings.assets["pony"].reference}}}
    run = new_run(project, session, uuid4(), graph)
    assert run.model_sha256 == session.settings.assets["pony"].sha256
    graph["4"]["inputs"]["ckpt_name"] = session.settings.assets["sd15"].reference
    assert new_run(project, session, uuid4(), graph).model_sha256 is None
