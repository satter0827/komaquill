"""Verify product exit and diagnostic contracts without provider access."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from komaquill import cli, colab_entry
from komaquill.events import failure_info
from komaquill.workflow import resource_json


@pytest.mark.parametrize("exit_code", [0, 7])
def test_official_cli_exit_result_reaches_the_parent(monkeypatch, exit_code):
    class App:
        pretty_exceptions_enable = True

        def __call__(self, **kwargs):
            assert kwargs == {"standalone_mode": False}
            return exit_code

    monkeypatch.setattr(
        colab_entry.importlib, "import_module", lambda _: SimpleNamespace(app=App())
    )
    with pytest.raises(SystemExit) as result:
        colab_entry.main()
    assert result.value.code == exit_code


def test_response_validation_is_not_a_storage_failure():
    class Response(BaseModel):
        count: int

    with pytest.raises(ValidationError) as result:
        Response.model_validate({"count": "private-invalid-value"})
    failure = failure_info(result.value, "response_validation")
    assert failure.code == "invalid_response"
    assert not failure.retryable
    assert "private-invalid-value" not in failure.model_dump_json()


def test_unexpected_failures_are_not_reported_as_storage_errors():
    failure = failure_info(RuntimeError("private-value"), "preparation")
    assert failure.code == "internal_error" and not failure.retryable


def test_doctor_rejects_a_malformed_node_response_safely(project, client, session, monkeypatch):
    @contextmanager
    def connected(_):
        yield client, session

    original = client.json
    nodes = {row["class_type"]: {} for row in resource_json("workflow-api.json").values()}
    monkeypatch.setattr(cli, "connected", connected)
    monkeypatch.setattr(
        client, "json", lambda route: nodes if route == "/object_info" else original(route)
    )
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "doctor"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output


@pytest.mark.parametrize("values", [None, {}, [], [None], [[1]]])
def test_invalid_checkpoint_choices_are_a_typed_response_error(client, monkeypatch, values):
    from komaquill.errors import KomaquillError

    monkeypatch.setattr(
        client,
        "json",
        lambda _: {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": values}}}},
    )
    with pytest.raises(KomaquillError) as error:
        client.checkpoints()
    assert error.value.code == "invalid_response" and not error.value.retryable
