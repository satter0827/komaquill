"""Verify library contracts within the declared test scope."""

import pytest
from komaquill.adapters.workflows import prepare


def test_workspace_profile_overrides_and_frozen_snapshot(project, monkeypatch):
    (project.root / "komaquill.toml").write_text(
        'models=["wai-illustrious"]\n[presets.klein-4b-edit]\nsteps=9\n', encoding="utf-8"
    )
    bound = project.configured()
    assert bound.settings().presets["klein-4b-edit"].steps == 9
    monkeypatch.setenv("KOMAQUILL_MODELS", '["klein-4b"]')
    assert project.settings().profiles == ("klein-4b", "klein-4b-edit")
    assert bound.settings().profiles == ("wai-illustrious", "wai-illustrious-img2img")


def test_graph_provenance_uses_the_actual_five_resources(project, session):
    from uuid import uuid4

    from komaquill.application.images.generation import new_run

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

    from komaquill.application.images.generation import new_run

    active = session.model_copy(update={"settings": project.settings()})
    run = new_run(project, active, uuid4(), {"4": {"inputs": inputs}})
    assert run.model_sha256 is None and not run.asset_sha256
