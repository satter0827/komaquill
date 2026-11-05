"""Verify platform contracts within the declared test scope."""

from types import SimpleNamespace

import pytest
from komaquill.adapters import colab_entry as colab_entry
from komaquill.domain.errors import failure_info
from pydantic import BaseModel, ValidationError


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
