"""Verify images contracts within the declared test scope."""

import pytest


@pytest.mark.parametrize("model", ["unknown-model", "wai-illustrious"])
def test_material_model_rejection_is_typed_before_file_import(project, model):
    from komaquill.api import Client
    from komaquill.domain.errors import KomaquillError
    from komaquill.interfaces.cli.image_inputs import material_inputs

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

    from komaquill.domain.errors import KomaquillError
    from komaquill.interfaces.cli import main as cli

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
