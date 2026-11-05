"""Verify images contracts within the declared test scope."""

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import assets, prepare
from komaquill.domain.configuration import RetainedRuntimeSettings
from komaquill.domain.models import Session
from pydantic import ValidationError


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
