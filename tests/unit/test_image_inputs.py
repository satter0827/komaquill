"""Verify portable prompts and rejection before any image request."""

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import prepare
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import CompiledImageInput, compile_image_input
from pydantic import ValidationError


@pytest.mark.parametrize("enabled", [True, False])
def test_effective_prompt_preserves_user_text_and_applies_quality_once(enabled):
    profile = (
        Settings(models=["klein-4b"])
        .presets["klein-4b"]
        .model_copy(update={"quality_enabled": enabled})
    )
    value = compile_image_input(
        profile, "雨の日の青い傘", subjects=("woman", "kitten"), common_prompt="rain"
    )
    assert value.prompt == "雨の日の青い傘"
    assert value.common_prompt == "rain"
    assert value.body == "rain, 雨の日の青い傘, woman, kitten"
    assert value.positive == (profile.positive_prefix + ", " if enabled else "") + value.body
    assert CompiledImageInput.model_validate_json(value.model_dump_json()) == value
    assert value.matches(profile)


@pytest.mark.parametrize("negative", ["", "extra limbs"])
def test_klein_explicit_negative_is_rejected_before_submission(negative):
    with pytest.raises(ValueError):
        compile_image_input(
            Settings(models=["klein-4b"]).presets["klein-4b"], "a kitten", negative=negative
        )


@pytest.mark.parametrize("negative,expected", [(None, None), ("", ""), ("blur", "blur")])
def test_wai_negative_defaults_replacement_and_disabling(negative, expected):
    profile = Settings(models=["klein-4b"]).presets["wai-illustrious"]
    compiled = compile_image_input(profile, "a park", negative=negative)
    assert compiled.negative == (profile.negative_default if expected is None else expected)
    assert compiled.matches(profile)


def test_frozen_prompt_is_used_without_recompilation():
    settings = Settings(models=["klein-4b"])
    profile = settings.presets["klein-4b"]
    compiled = compile_image_input(profile, "a kitten", subjects=("gray fur",))
    graph, _ = prepare("ignored", 42, "test", settings=settings, compiled_input=compiled)
    binding = next(row for row in profile.bindings if row.source == "prompt")
    assert graph[binding.api_node]["inputs"][binding.api_input] == compiled.body
    changed = settings.model_copy(
        update={
            "presets": {
                **settings.presets,
                "klein-4b": profile.model_copy(update={"positive_prefix": "other"}),
            }
        }
    )
    with pytest.raises(KomaquillError):
        prepare("ignored", 42, "test", settings=changed, compiled_input=compiled)


def test_inconsistent_retained_text_is_rejected_and_global_subjects_are_preserved():
    profile = Settings(models=["klein-4b"]).presets["klein-4b"]
    value = compile_image_input(profile, "a kitten")
    with pytest.raises(ValidationError):
        CompiledImageInput.model_validate({**value.model_dump(), "positive": "changed"})
    group = compile_image_input(profile, "group photograph", subjects=("one", "two", "three"))
    assert group.subject_prompts == ("one", "two", "three")
