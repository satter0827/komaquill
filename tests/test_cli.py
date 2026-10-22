"""VS Codeに依存しない操作と画面利用中の自動保存を検証する。"""

from contextlib import contextmanager

from typer.testing import CliRunner

from komaquill import cli
from komaquill.storage import load_runs
from komaquill.workflow import resource_json


def test_missing_connection_has_actionable_error(project):
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "collect"])
    assert result.exit_code == 1
    assert "start" in result.output
    assert "Traceback" not in result.output


def test_invalid_view_does_not_open_a_browser():
    result = CliRunner().invoke(cli.app, ["open", "--view", "unknown"])
    assert result.exit_code == 2


def test_open_uses_the_selected_view_and_collects(project, client, session, monkeypatch):
    @contextmanager
    def connection(_project):
        yield client, session

    opened = []
    monkeypatch.setattr(cli, "connected", connection)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    client.submit(resource_json("workflow-api.json"), resource_json("workflow.json"), "ui-client")

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)
    result = CliRunner().invoke(
        cli.app, ["--project", str(project.root), "open", "--view", "graph"]
    )
    assert result.exit_code == 0, result.output
    assert "template=sd15_graph" in opened[0]
    assert "source=komaquill" in opened[0]
    assert load_runs(project)[0].state == "saved"
    assert "Colab" in result.output
