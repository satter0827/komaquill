"""Verify purpose conditioning is frozen once before image submission."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.configuration import Settings
from komaquill.api import Client
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import preset_fingerprint
from komaquill.domain.workflow import digest

pytestmark = pytest.mark.usefixtures("scripted_services")


@pytest.mark.parametrize("negative", [None, "", "blur"])
def test_banner_conditions_are_recorded_and_reconfirmation_never_resubmits(
    project, session, client, stub, monkeypatch, negative
):
    settings = Settings(models=["wai-illustrious"])
    active = session.model_copy(update={"settings": settings, "backend": "external"})
    atomic_json(project.session_file, active.model_dump(mode="json"))

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr("komaquill.api.images.connected", connected)
    api = Client.initialize(project.root)
    identity = uuid4()
    run = api.images.submit(
        "An empty bookshop", run_id=identity, seed=7, purpose="banner", negative=negative
    )
    profile = settings.presets["wai-illustrious"]
    assert run.compiled_input.prompt == "scenery, no humans, environment only, An empty bookshop"
    expected = negative if negative is not None else profile.negative_default
    assert run.compiled_input.negative == ", ".join(
        part for part in (expected, profile.purpose_negatives["banner"]) if part
    )
    assert {source.name for source in run.input_sources} == {
        "purpose-banner-positive.txt",
        "purpose-banner-negative.txt",
    }
    assert run.compiled_input.matches(profile)
    assert (
        api.images.submit(
            "An empty bookshop", run_id=identity, seed=7, purpose="banner", negative=negative
        )
        == run
    )
    assert stub.submissions == 1
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.images.submit("An empty bookshop", run_id=identity, seed=7, purpose="portrait")
    saved = api.images.wait(identity)
    assert saved.state == "saved" and saved.compiled_input == run.compiled_input
    assert stub.submissions == 1


def test_positive_only_model_does_not_receive_negative_purpose_constraints(
    project, session, client, monkeypatch
):
    settings = Settings(models=["klein-4b"])
    profile = settings.presets["klein-4b"].model_copy(
        update={
            "purpose_prefixes": {"banner": "scenery"},
            "purpose_negatives": {"banner": "people"},
        }
    )
    settings.presets["klein-4b"] = profile
    active = session.model_copy(update={"settings": settings, "backend": "external"})
    atomic_json(project.session_file, active.model_dump(mode="json"))

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr("komaquill.api.images.connected", connected)
    run = Client.initialize(project.root).images.generate("A garden", seed=7, purpose="banner")
    assert run.compiled_input.negative is None
    assert run.compiled_input.prompt == "scenery, A garden"
    assert [source.role for source in run.input_sources] == ["positive"]


def test_empty_purpose_settings_preserve_retained_preset_hashes():
    profile = Settings(models=["klein-4b"]).presets["klein-4b"]
    original = profile.model_dump(mode="json")
    assert "purpose_prefixes" not in original and "purpose_negatives" not in original
    assert preset_fingerprint(profile) == digest(original)
    assert type(profile).model_validate(original).model_dump(mode="json") == original
