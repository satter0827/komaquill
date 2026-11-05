"""Reject malformed configuration before any resource can be started."""

from komaquill.interfaces.cli.main import app
from typer.testing import CliRunner


def test_malformed_workspace_configuration_has_a_safe_error(project):
    (project.root / "komaquill.toml").write_text("private = [")
    result = CliRunner().invoke(app, ["--project", str(project.root), "start"])
    assert result.exit_code == 1 and "Traceback" not in result.output
    assert "configuration" in result.output
