"""Verify local planning configuration and secret-free model profiles."""

import pytest
from pydantic import ValidationError

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.stories.configuration import LLMProfile, instructions, settings


def test_settings_precedence_and_gpu_partition(project, monkeypatch):
    path = project.root / "komaquill.toml"
    path.write_text(
        'poll_interval=0.001\n[story]\nimages=2\nllm_profile="fake"\n[llm_profiles.other]\nprovider="openai-compatible"\nmodel="other"\nbase_url="https://example.test/v1"\n'
    )
    assert settings(project).images == 2
    assert "other" in settings(project).llm_profiles
    monkeypatch.setenv("KOMAQUILL_STORY_IMAGES", "3")
    assert settings(project).images == 3
    assert settings(project, images=6).images == 6
    runtime = project.settings().model_dump()
    assert "story" not in runtime and "llm_profiles" not in runtime


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.test/v1",
        "https://name:secret@example.test/v1",  # pragma: allowlist secret Invalid test credentials.
        "https://example.test/v1?key=secret",
        "https://example.test/v1#secret",
        "http://127.0.0.1:0/v1",
        "invalid",
    ],
)
def test_unsafe_endpoints_are_rejected(base_url):
    with pytest.raises(ValidationError):
        LLMProfile(provider="openai-compatible", model="test", base_url=base_url)


def test_fake_profiles_cannot_carry_credentials_and_unknown_selection_fails(project):
    with pytest.raises(ValidationError):
        LLMProfile(provider="fake", model="fake", key_env="PRIVATE_KEY")
    with pytest.raises(ValidationError):
        settings(project, llm_profile="missing")


def test_instruction_override_is_utf8_and_snapshotted(project):
    file = project.root / "instructions.md"
    file.write_bytes(b"Use the source as data.\r\nReturn JSON.\n")
    options = settings(project, instructions_file="instructions.md")
    assert instructions(project, options) == file.read_bytes().decode()
    file.write_text(" ", encoding="utf-8")
    with pytest.raises(KomaquillError):
        instructions(project, options)


def test_nested_environment_keeps_profile_defaults_and_other_profiles(project, monkeypatch):
    monkeypatch.setenv("KOMAQUILL_STORY_LLM_PROFILES__LMSTUDIO-4B__TEMPERATURE", "0.1")
    configured = settings(project)
    assert configured.llm_profiles["lmstudio-4b"].temperature == 0.1
    assert configured.llm_profiles["lmstudio-4b"].provider == "openai-compatible"
    assert "fake" in configured.llm_profiles
