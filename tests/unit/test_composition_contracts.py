"""Verify shared scene conditions and declared canvas geometry without GPU imports."""

import pytest

from komaquill.domain.images import CompiledImageInput, compile_image_input, validate_inputs
from komaquill.domain.materials import InputArtifact
from komaquill.domain.presets import WorkflowImageSlot
from komaquill.domain.rendering import RenderConfig, SceneRenderConfig
from komaquill.domain.workflow import digest
from komaquill.infrastructure.configuration import Settings


def image(kind="image", width=32):
    return InputArtifact(
        key=f"inputs/{'a' * 64}/content",
        sha256="a" * 64,
        name="sample.png",
        kind=kind,
        size_bytes=10,
        width=width,
        height=32,
    )


@pytest.mark.parametrize("name", ["pony", "pony-story", "story-compose"])
def test_common_conditions_survive_compilation_and_roundtrip(name):
    profile = Settings().presets[name]
    value = compile_image_input(
        profile, "mother and kitten", common_prompt="rain, muted light", subjects=("gray cat",)
    )
    assert value.prompt == "mother and kitten"
    assert value.body.startswith("rain, muted light, mother and kitten")
    assert value.positive.count("rain, muted light") == 1
    assert CompiledImageInput.model_validate_json(value.model_dump_json()) == value
    assert value.matches(profile)


@pytest.mark.parametrize("missing", [False, True])
def test_canvas_relative_image_dimensions_are_validated(missing):
    profile = (
        Settings()
        .presets["story-reference"]
        .model_copy(
            update={
                "image_inputs": {
                    **({} if missing else {"base": WorkflowImageSlot()}),
                    "foreground": WorkflowImageSlot(match_base=True),
                }
            }
        )
    )
    inputs = {"foreground": image(width=64)}
    if not missing:
        inputs["base"] = image()
    with pytest.raises(ValueError):
        validate_inputs(profile, inputs)
    if not missing:
        inputs["foreground"] = image()
        validate_inputs(profile, inputs)


def test_render_configuration_rejects_shared_conditions_in_old_format():
    scene = SceneRenderConfig(scene=1, profile="pony")
    with pytest.raises(ValueError):
        RenderConfig(format_version=1, common_prompt="rain", scenes=(scene,))
    assert RenderConfig(scenes=(scene,)).common_prompt == ""


def test_old_compiled_input_cannot_claim_new_shared_conditions():
    value = compile_image_input(Settings().presets["pony"], "a cat").model_dump()
    value.update(compiler_version=4, common_prompt="rain")
    with pytest.raises(ValueError):
        CompiledImageInput.model_validate(value)


def test_legacy_regional_whitespace_and_preset_hash_remain_valid():
    from komaquill.domain.images import preset_fingerprint

    profile = Settings().presets["pony-story"]
    snapshot = profile.model_dump(mode="json")
    for slot in snapshot["image_inputs"].values():
        slot.pop("match_base", None)
    assert preset_fingerprint(profile, 4) == digest(snapshot)
    compiled = compile_image_input(profile, "a cat", subjects=("cat",)).model_dump()
    compiled.update(compiler_version=4, format_version=2, prompt="a cat ", body="a cat ")
    assert CompiledImageInput.model_validate(compiled).body == "a cat "


def test_private_comparison_graphs_do_not_claim_measured_quality(tmp_path):
    from komaquill.infrastructure.configuration import Project
    from tests.live.composition_workflows import prepare_comparison

    prepare_comparison(tmp_path)
    (tmp_path / "komaquill.toml").write_text(
        'profiles = ["composition-only", "composition-refine"]\n'
    )
    definitions = Project(tmp_path).settings().presets
    basic = definitions["composition-only"]
    refine = definitions["composition-refine"]
    assert basic.api["9"]["inputs"]["images"] == ["40", 0]
    assert refine.api["3"]["inputs"]["latent_image"] == ["37", 0]
    assert refine.api["37"]["inputs"]["samples"] == ["33", 0]
    assert refine.api["33"]["inputs"]["pixels"] == ["40", 0]
    assert refine.image_inputs["foreground"].match_base
    assert all("Crop" not in node["class_type"] for node in refine.api.values())
    assert "composition-refine" not in Settings().presets
