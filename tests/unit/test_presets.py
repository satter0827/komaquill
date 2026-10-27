"""Validate standard Klein exports and immutable resource selection."""

import pytest
from pydantic import ValidationError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import assets, prepare, resource_json, select


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
    settings = Settings()
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


def test_workspace_profile_overrides_and_frozen_snapshot(project, monkeypatch):
    (project.root / "komaquill.toml").write_text(
        'models=["wai-illustrious"]\n[presets.klein-4b-edit]\nsteps=9\n', encoding="utf-8"
    )
    bound = project.configured()
    assert bound.settings().presets["klein-4b-edit"].steps == 9
    monkeypatch.setenv("KOMAQUILL_MODELS", '["klein-4b"]')
    assert project.settings().profiles == ("klein-4b", "klein-4b-edit")
    assert bound.settings().profiles == ("wai-illustrious", "wai-illustrious-img2img")


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
    model = Settings().assets["klein-4b"].model_dump()
    with pytest.raises(ValidationError):
        ModelPreset.model_validate({**model, **changes})


def test_graph_provenance_uses_the_actual_five_resources(project, session):
    from uuid import uuid4

    from komaquill.application.generation import new_run

    active = session.model_copy(update={"settings": project.settings()})
    graph, _ = prepare("a kitten", 42, "test", settings=active.settings)
    run = new_run(project, active, uuid4(), graph)
    assert active.settings.assets["klein-4b"].sha256 in run.asset_sha256.values()
    assert len(run.asset_sha256) == 4
    graph["4"]["inputs"]["unet_name"] = "unregistered.safetensors"
    assert (
        active.settings.assets["klein-4b"].sha256
        not in new_run(project, active, uuid4(), graph).asset_sha256.values()
    )


@pytest.mark.parametrize("inputs", [None, [], "invalid", {"unet_name": "unknown.safetensors"}])
def test_graph_provenance_handles_malformed_inputs(project, session, inputs):
    from uuid import uuid4

    from komaquill.application.generation import new_run

    active = session.model_copy(update={"settings": project.settings()})
    run = new_run(project, active, uuid4(), {"4": {"inputs": inputs}})
    assert run.model_sha256 is None and not run.asset_sha256
