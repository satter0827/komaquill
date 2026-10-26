"""Check the fixed Klein graphs and standard resource boundaries."""

import pytest
from pydantic import ValidationError

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.presets import GenerationPreset
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import prepare


@pytest.mark.parametrize("name", ["klein", "klein-edit"])
def test_both_loras_feed_the_sampler_and_share_the_uncensored_encoder(name):
    settings = Settings()
    graph, ui = prepare("a kitten", None, 42, "test", settings=settings, profile_name=name)
    assert graph["90"]["inputs"]["model"] == ["4", 0]
    assert graph["91"]["inputs"]["model"] == ["90", 0]
    assert graph["63"]["inputs"]["model"] == ["91", 0]
    assert graph["71"]["inputs"]["clip_name"] == settings.assets["klein-encoder"].reference
    assert graph["90"]["inputs"]["strength_model"] == 1.0
    assert graph["91"]["inputs"]["strength_model"] == 1.0
    assert ui["extra"]["linearData"]["outputs"] == ["9"]
    if name == "klein-edit":
        assert graph["121"]["inputs"]["latent"] == ["82", 0]
        assert graph["123"]["inputs"]["latent"] == ["82", 0]
        assert ["77", "image"] in ui["extra"]["linearData"]["inputs"]


def test_resource_role_mismatch_fails_before_allocation():
    data = Settings().model_dump()
    data["presets"]["klein"]["resources"]["encoder"] = "klein-vae"
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(data)


@pytest.mark.parametrize(
    "parameters",
    [
        {"party_strength": True},
        {"fixes_strength": 100.0},
        {"party_strength": float("nan")},
        {"unknown": 1.0},
    ],
)
def test_invalid_lora_parameters_are_rejected(parameters):
    with pytest.raises(ValueError):
        Settings().presets["klein"].configured_parameters(parameters)


def test_resource_bindings_must_target_real_standard_model_inputs():
    data = Settings().presets["klein"].model_dump()
    data["bindings"][0]["api_input"] = "missing"
    with pytest.raises(ValidationError):
        GenerationPreset.model_validate(data)
