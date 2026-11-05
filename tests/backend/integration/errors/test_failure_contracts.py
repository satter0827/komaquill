"""Verify platform contracts within the declared test scope."""

from contextlib import contextmanager

from komaquill.adapters import colab_entry as colab_entry
from komaquill.adapters.workflows import resource_json
from komaquill.interfaces.cli import main as cli
from typer.testing import CliRunner


def test_doctor_rejects_a_malformed_node_response_safely(project, client, session, monkeypatch):
    @contextmanager
    def connected(_):
        yield client, session

    original = client.json
    nodes = {
        row["class_type"]: {}
        for row in resource_json("presets/klein-4b/workflow-api.json").values()
    }
    monkeypatch.setattr(cli, "connected", connected)
    monkeypatch.setattr(
        client, "json", lambda route: nodes if route == "/object_info" else original(route)
    )
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "doctor"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
