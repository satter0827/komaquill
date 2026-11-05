"""Verify images contracts within the declared test scope."""

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import prepare


def test_model_selection_prepares_only_active_resource_dependencies():
    from komaquill.adapters.workflows import assets

    settings = Settings(models=["klein-4b", "wai-illustrious"])
    assert len(assets(settings)) == 6
    assert all(asset.model_family != "klein-9b" for asset in assets(settings))
    assert "klein-9b" in {p.model_id for p in settings.presets.values()}


@pytest.mark.parametrize("value", [0, 0.5, 1.0])
def test_wai_one_strength_input_controls_both_loader_branches(value):
    settings = Settings(models=["wai-illustrious"])
    profile = settings.presets["wai-illustrious"].configured_parameters({"detail_strength": value})
    settings = settings.model_copy(
        update={"presets": {**settings.presets, "wai-illustrious": profile}}
    )
    graph, _ = prepare("an empty room", 42, "test", settings=settings)
    assert graph["92"]["inputs"]["value"] == value
    assert (
        graph["90"]["inputs"]["strength_model"]
        == graph["90"]["inputs"]["strength_clip"]
        == ["92", 0]
    )
