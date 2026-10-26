"""Keep native graph editing accessible without disrupting automatic collection."""

import json

import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import views


@pytest.mark.parametrize("result", [False, "exception", True])
def test_automatic_browser_failure_retains_connection_and_reports_url(
    project, session, monkeypatch, result
):
    session.settings = project.settings()

    def browser(url):
        if result == "exception":
            raise OSError("private browser details")
        return result

    monkeypatch.setattr(views.webbrowser, "open", browser)
    output = []
    assert views.open_view(project, session, echo=output.append, strict=False) == (result is True)
    assert output
    if result is not True:
        assert session.endpoint in output[0]
        with pytest.raises(KomaquillError):
            views.open_view(project, session)


def test_native_graph_uses_selected_prepared_profile(project, session, monkeypatch):
    session.settings = project.settings().model_copy(update={"profiles": ("klein", "klein")})
    urls = []
    monkeypatch.setattr(views.webbrowser, "open", lambda url: urls.append(url) or True)
    views.open_view(project, session, "graph", "klein")
    assert "template=klein_graph" in urls[-1]
    session.backend = "external"
    views.open_view(project, session, "graph", "klein")
    exported = json.loads((project.runtime / "workflows/klein_graph.json").read_bytes())
    assert exported["extra"]["linearMode"] is False
    assert any(n["type"] == "LoraLoaderModelOnly" for n in exported["nodes"])
