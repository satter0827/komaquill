"""Check shared story inputs and the six standard model-operation workflows."""

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import prepare


def test_model_selection_prepares_only_active_resource_dependencies():
    from komaquill.adapters.workflows import assets

    settings = Settings(models=["klein-4b", "wai-illustrious"])
    assert len(assets(settings)) == 6
    assert all(asset.model_family != "klein-9b" for asset in assets(settings))
    assert "klein-9b" in {p.model_id for p in settings.presets.values()}


@pytest.mark.parametrize("value", [0, 0.5, 1.0])
def test_wai_one_strength_input_controls_both_loader_branches(value):
    settings = Settings(models=["wai-illustrious"])
    profile = settings.presets["wai-illustrious"].configured_parameters({"detail_strength": value})
    settings = settings.model_copy(
        update={"presets": {**settings.presets, "wai-illustrious": profile}}
    )
    graph, _ = prepare("an empty room", 42, "test", settings=settings)
    assert graph["92"]["inputs"]["value"] == value
    assert (
        graph["90"]["inputs"]["strength_model"]
        == graph["90"]["inputs"]["strength_clip"]
        == ["92", 0]
    )


@pytest.mark.parametrize("model", ["unknown-model", "wai-illustrious"])
def test_material_model_rejection_is_typed_before_file_import(project, model):
    from komaquill.api import Client
    from komaquill.cli.image_inputs import material_inputs
    from komaquill.domain.errors import KomaquillError

    with pytest.raises(KomaquillError) as caught:
        material_inputs(
            Client(project.root), model, {"reference": "not-read.png"}, project.root, "edit"
        )
    assert caught.value.code == "invalid_configuration"
    assert not caught.value.retryable
    assert not (project.data / "inputs").exists()


def test_open_rejects_unknown_model_with_the_same_typed_failure(project, session, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from komaquill.cli import main as cli
    from komaquill.domain.errors import KomaquillError

    @contextmanager
    def operation(_):
        yield project

    @contextmanager
    def connected(_):
        yield (
            SimpleNamespace(json=lambda _: {}),
            session.model_copy(update={"settings": project.settings()}),
        )

    monkeypatch.setattr(cli, "operation", operation)
    monkeypatch.setattr(cli, "connected", connected)
    monkeypatch.setattr(cli, "verify_runtime", lambda *args: None)
    with pytest.raises(KomaquillError) as caught:
        cli.open_command(None, model="unknown-model")
    assert caught.value.code == "invalid_configuration" and not caught.value.retryable
