"""Verify library contracts within the declared test scope."""

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import assets, prepare, resource_json, select
from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset
from pydantic import ValidationError


@pytest.mark.parametrize("name", ["klein-4b", "klein-4b-edit", "klein-9b", "klein-9b-edit"])
def test_profile_exports_match_api_connections_and_widget_values(name):
    settings = Settings(models=[name.removesuffix("-edit")])
    graph, ui = prepare("a blue lake", 42, "request", settings=settings, profile_name=name)
    profile = settings.presets[name]
    nodes = {str(node["id"]): node for node in ui["nodes"]}
    links = {row[0]: row for row in ui["links"]}
    for identity, data in graph.items():
        assert data["class_type"] == nodes[identity]["type"]
        for slot in nodes[identity].get("inputs", []):
            if slot["link"] is None:
                continue
            link = links[slot["link"]]
            assert data["inputs"][slot["name"]] == [str(link[1]), link[2]]
            assert link[4] == nodes[identity]["inputs"].index(slot)
            assert link[0] in nodes[str(link[1])]["outputs"][link[2]]["links"]
    for binding in profile.bindings:
        assert (
            graph[binding.api_node]["inputs"][binding.api_input]
            == nodes[str(binding.ui_node)]["widgets_values"][binding.ui_widget]
        )
    for binding in profile.bindings:
        if binding.source in profile.resources:
            assert (
                graph[binding.api_node]["inputs"][binding.api_input]
                == settings.assets[profile.resources[binding.source]].reference
            )
    assert graph["73"]["inputs"]["noise_seed"] == 42
    assert graph["62"]["inputs"]["steps"] == profile.steps


def test_selected_profiles_deduplicate_all_five_shared_resources():
    settings = Settings(models=["klein-4b"])
    assert set(settings.presets) == {
        "klein-4b",
        "klein-4b-edit",
        "klein-9b",
        "klein-9b-edit",
        "wai-illustrious",
        "wai-illustrious-img2img",
    }
    assert len(assets(settings)) == 4
    with pytest.raises(KomaquillError):
        select(Settings(models=["klein-4b"]), "wai-illustrious")
    with pytest.raises(KomaquillError):
        resource_json("../workflow.json")


@pytest.mark.parametrize(
    "overrides",
    [
        {"profiles": []},
        {"profiles": ["klein-4b", "klein-4b"]},
        {"profiles": ["unknown"]},
        {"presets": {"klein-4b": {"resources": {"diffusion": "missing"}}}},
        {"presets": {"klein-4b": {"resources": {"diffusion": "klein-vae"}}}},
        {"presets": {"bad/name": {}}},
    ],
)
def test_invalid_profile_selection_fails_before_allocation(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "../a"},
        {"filename": ".."},
        {"filename": "C:outside"},
        {"filename": "a\\b"},
        {"url": "http://example.com/a"},
        {"url": None, "repo_id": "", "revision": ""},
        {"repo_id": "owner/repository", "revision": "a" * 40},
    ],
)
def test_unsafe_or_ambiguous_asset_sources_are_rejected(changes):
    model = Settings(models=["klein-4b"]).assets["klein-4b"].model_dump()
    with pytest.raises(ValidationError):
        ModelPreset.model_validate({**model, **changes})
