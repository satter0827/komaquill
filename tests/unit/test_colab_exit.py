"""Preserve exit codes from both CLI libraries used by the pinned Colab client."""

from types import SimpleNamespace

import click
import pytest
import typer

from komaquill.infrastructure import colab_entry


@pytest.mark.parametrize("exception", [typer.Exit, click.exceptions.Exit])
def test_cli_exit_does_not_become_an_internal_or_authentication_failure(
    monkeypatch, capsys, exception
):
    class App:
        pretty_exceptions_enable = True

        def __call__(self, **kwargs):
            raise exception(2)

    monkeypatch.setattr(
        colab_entry.importlib, "import_module", lambda _: SimpleNamespace(app=App())
    )
    with pytest.raises(SystemExit) as result:
        colab_entry.main()
    assert result.value.code == 2
    assert capsys.readouterr() == ("", "")
