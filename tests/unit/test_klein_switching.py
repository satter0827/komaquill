"""Check model-family selection and the removal of new negative inputs."""

from inspect import signature

import pytest
from pydantic import ValidationError

from komaquill.api import Client, RenderConfig, SceneRenderConfig, services
from komaquill.domain.configuration import RetainedRuntimeSettings
from komaquill.domain.images import ImageInput
from komaquill.domain.models import Session
from komaquill.domain.status import SessionStatus
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import assets, prepare


@pytest.mark.parametrize("name", ["klein-4b", "klein-4b-edit", "klein-9b", "klein-9b-edit"])
def test_profile_prepares_exactly_its_five_assets_and_positive_guider(name):
    settings = Settings(models=[name.removesuffix("-edit")])
    selected = assets(settings)
    assert len(selected) == 4
    assert {item.model_family for item in selected} == {name.removesuffix("-edit"), "flux2"}
    graph, _ = prepare("opaque clothes, blue bag", 42, "test", settings=settings, profile_name=name)
    assert graph["63"]["class_type"] == "BasicGuider"
    assert set(graph["63"]["inputs"]) == {"model", "conditioning"}
    assert "76" not in graph and "123" not in graph


@pytest.mark.parametrize("role", ["diffusion", "encoder", "aniedit"])
def test_cross_family_resources_are_rejected_before_allocation(role):
    data = Settings().model_dump()
    data["presets"]["klein-9b"]["resources"][role] = data["presets"]["klein-4b"]["resources"][role]
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


@pytest.mark.parametrize("value", [None, "", "extra hands"])
def test_input_contracts_reject_removed_fields(value):
    with pytest.raises(ValidationError):
        ImageInput.model_validate({"prompt": "a street", "profile": value})
    with pytest.raises(ValidationError):
        SceneRenderConfig.model_validate({"scene": 1, "profile": "klein-4b", "negative": value})
    assert RenderConfig.model_fields["format_version"].default == 4


def test_public_interfaces_have_no_negative_parameter(project):
    api = Client(project.root)
    for operation in (api.images.generate, api.stories.generate, api.stories.render):
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
    monkeypatch.setattr(services.SessionService, "status", lambda _: SessionStatus())
    api = Client(project.root)
    api.sessions.start(gpu="A100", high_ram=True, models=["klein-9b"])
    snapshot = captured[0]
    assert (snapshot.gpu, snapshot.high_ram) == ("A100", True)
    assert snapshot.profiles == ("klein-9b", "klein-9b-edit")
    monkeypatch.setenv("KOMAQUILL_GPU", "T4")
    assert captured[0] == snapshot


def test_former_settings_remain_readable_without_becoming_executable():
    from uuid import uuid4

    value = Settings().model_dump(mode="json")
    value.pop("format_version")
    value.pop("models")
    value["profiles"] = ["klein-4b", "klein-4b-edit"]
    value["presets"] = {
        key: row for key, row in value["presets"].items() if key in value["profiles"]
    }
    for row in value["presets"].values():
        for key in ("model_id", "operation", "negative_default", "adjustable"):
            row.pop(key, None)
    for key in ("high_ram", "compute_unit_limit", "compute_unit_reserve"):
        value.pop(key)
    for asset in value["assets"].values():
        asset.pop("model_family")
    for profile in value["presets"].values():
        profile.pop("format_version")
        profile.pop("model_family")
        profile.update(cfg=1.0, negative="", negative_prefix="")
    session = Session.model_validate(
        {"endpoint": "http://127.0.0.1:8188", "runtime_id": str(uuid4()), "settings": value}
    )
    assert isinstance(session.settings, RetainedRuntimeSettings)
    assert session.settings.presets["klein-4b"].cfg == 1.0
    assert session.model_dump(mode="json")["settings"] == value
    assert session.model_dump()["settings"]["presets"]["klein-4b"]["negative"] == ""
    from komaquill.domain.errors import KomaquillError

    with pytest.raises(KomaquillError):
        prepare("a street", 42, "new", settings=session.settings)


def test_optional_settings_remain_exact_in_json_and_produce_valid_gpu_toml(project):
    import json
    import tarfile
    import tomllib

    from komaquill.domain.configuration import RuntimeSettings
    from komaquill.infrastructure.deployment import create_payload

    project = project.configured(
        project.settings().model_copy(update={"submission_timeout": None, "session_timeout": None})
    )
    payload = create_payload(project)
    with tarfile.open(payload.path) as archive:
        values = tomllib.loads(archive.extractfile("komaquill.toml").read().decode())
        snapshot = json.loads(archive.extractfile("manifest.json").read())
    assert "compute_unit_limit" not in values and "session_timeout" not in values
    assert RuntimeSettings.model_validate(snapshot["settings"]) == project.settings()
