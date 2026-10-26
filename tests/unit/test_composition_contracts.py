"""Verify shared scene conditions and declared canvas geometry without GPU imports."""

import pytest

from komaquill.domain.images import CompiledImageInput, compile_image_input, validate_inputs
from komaquill.domain.materials import InputArtifact
from komaquill.domain.presets import WorkflowImageSlot
from komaquill.domain.rendering import RenderConfig, SceneRenderConfig
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


@pytest.mark.parametrize("name", ["klein", "klein-edit"])
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
        .presets["klein"]
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
    scene = SceneRenderConfig(scene=1, profile="klein")
    with pytest.raises(ValueError):
        RenderConfig(format_version=1, common_prompt="rain", scenes=(scene,))
    assert RenderConfig(scenes=(scene,)).common_prompt == ""


def test_old_compiled_input_cannot_claim_new_shared_conditions():
    value = compile_image_input(Settings().presets["klein"], "a cat").model_dump()
    value.update(compiler_version=4, common_prompt="rain")
    with pytest.raises(ValueError):
        CompiledImageInput.model_validate(value)
