"""Verify images contracts within the declared test scope."""

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.application.images.generation import generation_settings


def test_bundled_defaults_and_explicit_override_are_independent():
    settings = Settings(models=["klein-4b"])
    assert settings.presets["klein-4b"].batch_size == 1
    assert settings.story_max_in_flight == 2
    changed = generation_settings(settings, "klein-4b", 4)
    assert changed.presets["klein-4b"].batch_size == 4
    assert settings.presets["klein-4b"].batch_size == 1


def test_invalid_default_candidate_limit_fails_before_runtime_allocation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(models=["klein-4b"], max_candidates=1, presets={"klein-4b": {"batch_size": 2}})
