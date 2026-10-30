"""Keep native graph editing accessible without disrupting automatic collection."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from komaquill.domain.errors import KomaquillError
from komaquill.runtime import views


@pytest.mark.parametrize("result", [False, "exception", "timeout", True])
def test_automatic_browser_failure_retains_connection_and_reports_url(
    project, session, monkeypatch, result
):
    session.settings = project.settings()

    def browser(args, **kwargs):
        assert kwargs["timeout"] == session.settings.control_timeout
        if result == "exception":
            raise OSError("private browser details")
        if result == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return SimpleNamespace(returncode=0 if result is True else 1)

    monkeypatch.setattr(views.subprocess, "run", browser)
    output = []
    assert views.open_view(project, session, echo=output.append, strict=False) == (result is True)
    assert output
    if result is not True:
        assert session.endpoint in output[0]
        with pytest.raises(KomaquillError):
            views.open_view(project, session)


def test_native_graph_uses_selected_prepared_profile(project, session, monkeypatch):
    session.settings = project.settings()
    urls = []
    monkeypatch.setattr(
        views.subprocess,
        "run",
        lambda args, **kwargs: urls.append(args[-1]) or SimpleNamespace(returncode=0),
    )
    views.open_view(project, session, "graph", "klein-4b")
    assert "template=klein-4b_graph" in urls[-1]
    session.backend = "external"
    views.open_view(project, session, "graph", "klein-4b")
    exported = json.loads((project.runtime / "workflows/klein-4b_graph.json").read_bytes())
    assert exported["extra"]["linearMode"] is False
    assert any(n["type"] == "LoraLoaderModelOnly" for n in exported["nodes"])
