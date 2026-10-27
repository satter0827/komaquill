"""Verify portable prompts and rejection before any image request."""

import pytest
from pydantic import ValidationError

from komaquill.application.generation import generate
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import CompiledImageInput, compile_image_input
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import prepare


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("negative", [None, ""])
def test_effective_prompt_preserves_user_text_and_applies_quality_once(enabled, negative):
    profile = Settings().presets["klein"].model_copy(update={"quality_enabled": enabled})
    value = compile_image_input(
        profile, "雨の日の青い傘", negative, subjects=("woman", "kitten"), common_prompt="rain"
    )
    assert value.prompt == "雨の日の青い傘"
    assert value.common_prompt == "rain"
    assert value.body == "rain, 雨の日の青い傘, woman, kitten"
    assert value.positive == (profile.positive_prefix + ", " if enabled else "") + value.body
    assert value.negative == ""
    assert value.layout == "global"
    assert CompiledImageInput.model_validate_json(value.model_dump_json()) == value
    assert value.matches(profile)


def test_negative_prompt_is_rejected_before_upload_or_submission(project, session, client, stub):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, "a kitten", "extra limbs", 42)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0
    assert stub.upload_calls == 0
    assert not project.repository.load()


def test_frozen_prompt_is_used_without_recompilation():
    settings = Settings()
    profile = settings.presets["klein"]
    compiled = compile_image_input(profile, "a kitten", subjects=("gray fur",))
    graph, _ = prepare("ignored", None, 42, "test", settings=settings, compiled_input=compiled)
    binding = next(row for row in profile.bindings if row.source == "prompt")
    assert graph[binding.api_node]["inputs"][binding.api_input] == compiled.body
    changed = settings.model_copy(
        update={
            "presets": {
                **settings.presets,
                "klein": profile.model_copy(update={"positive_prefix": "other"}),
            }
        }
    )
    with pytest.raises(KomaquillError):
        prepare("ignored", None, 42, "test", settings=changed, compiled_input=compiled)


def test_inconsistent_retained_text_is_rejected_and_global_subjects_are_preserved():
    profile = Settings().presets["klein"]
    value = compile_image_input(profile, "a kitten")
    with pytest.raises(ValidationError):
        CompiledImageInput.model_validate({**value.model_dump(), "positive": "changed"})
    group = compile_image_input(profile, "group photograph", subjects=("one", "two", "three"))
    assert group.subject_prompts == ("one", "two", "three")
