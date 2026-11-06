"""Verify images contracts within the declared test scope."""

from inspect import signature

import pytest
from komaquill.api import Client
from komaquill.api import runtime as services
from komaquill.domain.status import SessionStatus


def test_public_interfaces_have_no_negative_parameter(project):
    api = Client(project.root)
    for operation in (api.images.generate, api.images.submit):
        assert "profile" not in signature(operation).parameters
    assert "negative" in signature(api.images.generate).parameters


@pytest.mark.parametrize(
    "name,value,field,expected",
    [
        ("KOMAQUILL_HIGH_RAM", "true", "high_ram", True),
        ("KOMAQUILL_MAX_CANDIDATES", "6", "max_candidates", 6),
        ("KOMAQUILL_STORY_MAX_IN_FLIGHT", "3", "story_max_in_flight", 3),
    ],
)
def test_strict_environment_fields_are_decoded_at_the_text_boundary(
    project, monkeypatch, name, value, field, expected
):
    monkeypatch.setenv(name, value)
    assert getattr(project.settings(), field) == expected


def test_gpu_and_memory_overrides_are_frozen_with_cli_precedence(project, monkeypatch):
    monkeypatch.setenv("KOMAQUILL_GPU", "H100")
    captured = []
    monkeypatch.setattr(
        services.supervisor,
        "start",
        lambda project, *args, **kwargs: captured.append(project.settings()),
    )
    monkeypatch.setattr(services.RuntimeService, "status", lambda _: SessionStatus())
    api = Client(project.root)
    api.runtime.start(gpu="A100", high_ram=True, models=["klein-9b"])
    snapshot = captured[0]
    assert (snapshot.gpu, snapshot.high_ram) == ("A100", True)
    assert snapshot.profiles == ("klein-9b", "klein-9b-edit")
    monkeypatch.setenv("KOMAQUILL_GPU", "T4")
    assert captured[0] == snapshot


def test_optional_settings_remain_exact_in_json_and_produce_valid_gpu_toml(project):
    import json
    import tarfile
    import tomllib

    from komaquill.adapters.deployment import create_payload
    from komaquill.domain.configuration import RuntimeSettings

    project = project.configured(
        project.settings().model_copy(update={"submission_timeout": None, "session_timeout": None})
    )
    payload = create_payload(project)
    with tarfile.open(payload.path) as archive:
        values = tomllib.loads(archive.extractfile("komaquill.toml").read().decode())
        snapshot = json.loads(archive.extractfile("manifest.json").read())
    assert "compute_unit_limit" not in values and "session_timeout" not in values
    assert RuntimeSettings.model_validate(snapshot["settings"]) == project.settings()
