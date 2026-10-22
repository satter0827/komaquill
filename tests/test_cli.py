"""VS Codeに依存しない操作と画面利用中の自動保存を検証する。"""

from contextlib import contextmanager

from typer.testing import CliRunner

from komaquill import cli


def test_missing_connection_has_actionable_error(project):
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "collect"])
    assert result.exit_code == 1
    assert "start" in result.output
    assert "Traceback" not in result.output


def test_invalid_view_does_not_open_a_browser():
    result = CliRunner().invoke(cli.app, ["open", "--view", "unknown"])
    assert result.exit_code == 2


def test_open_returns_after_opening_the_selected_view(project, client, session, monkeypatch):
    @contextmanager
    def connection(_project):
        yield client, session

    opened = []
    monkeypatch.setattr(cli, "connected", connection)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    result = CliRunner().invoke(
        cli.app, ["--project", str(project.root), "open", "--view", "graph"]
    )
    assert result.exit_code == 0, result.output
    assert "template=sd15_graph" in opened[0]
    assert "source=komaquill" in opened[0]
    assert "start" in result.output


def test_status_without_a_session_never_creates_gpu(project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("確認でGPUを確保してはいけない")

    monkeypatch.setattr(cli.Colab, "acquire", forbidden)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0, result.output
    assert "未回収: 0件" in result.output
