"""Keep source precedence, feature validation and immutable workspace snapshots explicit."""

import pytest
from komaquill.adapters.configuration import Settings, WorkspaceConfiguration
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api import Client
from komaquill.domain.errors import KomaquillError
from pydantic import ValidationError


def test_sources_merge_profile_leaves_and_freeze_file_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "komaquill.toml"
    path.write_text(
        "poll_interval=2\n[conversation]\nrecent_turns=6\n"
        "[llm_profiles.lmstudio-4b]\ntemperature=0.7\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KOMAQUILL_POLL_INTERVAL", "3")
    monkeypatch.setenv("KOMAQUILL_CONVERSATION__RECENT_TURNS", "8")
    monkeypatch.setenv("KOMAQUILL_LLM_PROFILES__LMSTUDIO-4B__TEMPERATURE", "0")
    snapshot = WorkspaceConfiguration(tmp_path)
    path.write_text("poll_interval=99\n", encoding="utf-8")
    monkeypatch.setenv("KOMAQUILL_POLL_INTERVAL", "100")
    assert snapshot.runtime.poll_interval == 3
    assert snapshot.conversation.recent_turns == 8
    profile = snapshot.conversation.llm_profiles["lmstudio-4b"]
    assert profile.temperature == 0 and profile.model and profile.require_model_match


def test_invalid_llm_settings_do_not_block_runtime_or_saved_session_settings(tmp_path):
    (tmp_path / "komaquill.toml").write_text("[conversation]\nrecent_turns=0\n", encoding="utf-8")
    snapshot = WorkspaceConfiguration(tmp_path)
    assert snapshot.runtime.local_port > 0
    with pytest.raises(ValidationError):
        _ = snapshot.conversation
    (tmp_path / "komaquill.toml").write_text("invalid = [", encoding="utf-8")
    saved = Settings()
    restored = RuntimeWorkspace(tmp_path, saved).configured(saved)
    assert restored.settings() == saved


def test_client_shares_execution_across_roles_and_keeps_workspaces_separate(tmp_path):
    first = Client.initialize(tmp_path / "first")
    second = Client.initialize(tmp_path / "second")
    execution = first.library.creator.execution
    assert first.library.image_describer.execution is execution
    assert first.conversations.responder.execution is execution
    assert first.images._text_preparation.describer.execution is execution
    assert execution is not second.library.creator.execution
    assert execution.settings.recent_turns == 6
    (first.workspace / "komaquill.toml").write_text(
        "[conversation]\nrecent_turns=2\n", encoding="utf-8"
    )
    assert execution.settings.recent_turns == 6
    assert Client(first.workspace).conversations.responder.execution.settings.recent_turns == 2


def test_configuring_runtime_does_not_validate_unrelated_llm_settings(tmp_path):
    client = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text("[conversation]\nrecent_turns=0\n", encoding="utf-8")
    client.configure()
    assert client.runtime._project.settings().local_port > 0
    with pytest.raises(KomaquillError, match="invalid_configuration"):
        client.conversations.profiles()


def test_false_and_zero_override_defaults_and_empty_is_not_unspecified(tmp_path, monkeypatch):
    (tmp_path / "komaquill.toml").write_text("high_ram=true\n[conversation]\nrepair_attempts=1\n")
    monkeypatch.setenv("KOMAQUILL_HIGH_RAM", "false")
    monkeypatch.setenv("KOMAQUILL_CONVERSATION__REPAIR_ATTEMPTS", "0")
    snapshot = WorkspaceConfiguration(tmp_path)
    assert snapshot.runtime.high_ram is False
    assert snapshot.conversation.repair_attempts == 0
    monkeypatch.setenv("KOMAQUILL_HIGH_RAM", "")
    with pytest.raises(ValueError):
        _ = WorkspaceConfiguration(tmp_path).runtime


def test_image_and_llm_services_share_the_first_client_configuration(tmp_path):
    client = Client.initialize(tmp_path)
    path = tmp_path / "komaquill.toml"
    path.write_text('gpu="T4"\n[conversation]\nrecent_turns=4\n')
    client.images.configure()
    path.write_text('gpu="L4"\n[conversation]\nrecent_turns=2\n')
    assert client.conversations.responder.execution.settings.recent_turns == 4
    assert client.runtime._project.settings().gpu == "T4"
    assert Client(tmp_path).runtime._project.settings().gpu == "L4"
