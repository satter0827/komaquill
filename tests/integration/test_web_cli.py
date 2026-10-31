"""Verify the CLI shares durable conversation processing and safe error handling."""

import json

from komaquill.cli.main import app
from typer.testing import CliRunner


def test_cli_chat_create_send_read_and_failure_are_safe(project):
    runner = CliRunner()
    prefix = ["--project", str(project.root), "chat"]
    result = runner.invoke(
        app, [*prefix, "create", "CLI conversation", "--profile", "fake", "--locale", "en"]
    )
    assert result.exit_code == 0, result.output
    identity = json.loads(result.output)["id"]
    sent = runner.invoke(app, [*prefix, "send", identity, "Hello"])
    assert sent.exit_code == 0
    saved = runner.invoke(app, [*prefix, "get", identity])
    turns = json.loads(saved.output)["turns"]
    assert len(turns) == 1 and turns[0]["assistant"] in sent.output
    assert json.loads(runner.invoke(app, [*prefix, "list"]).output)["id"] == identity
    assert json.loads(runner.invoke(app, [*prefix, "status", identity]).output)["revision"] == 1
    changed = runner.invoke(
        app,
        [
            *prefix,
            "command",
            identity,
            "set_state",
            "--expected-revision",
            "1",
            "--state",
            '{"place":"garden"}',
        ],
    )
    assert changed.exit_code == 0, changed.output
    assert json.loads(changed.output)["state"] == {"place": "garden"}
    assert runner.invoke(app, [*prefix, "resume", identity]).exit_code == 0
    failed = runner.invoke(app, [*prefix, "send", identity, "Unknown"])
    assert failed.exit_code == 1 and "Traceback" not in failed.output
    (project.root / "komaquill.toml").write_text("invalid [")
    # Saved data remains readable after a configuration error.
    assert runner.invoke(app, [*prefix, "get", identity]).exit_code == 0


def test_cli_storage_failure_records_type_without_exception_content(project, monkeypatch):
    from komaquill.api.runtime import RuntimeService

    def fail(*args, **kwargs):
        raise PermissionError("private value must not be logged")

    monkeypatch.setattr(RuntimeService, "stop", fail)
    result = CliRunner().invoke(app, ["--project", str(project.root), "stop"])
    assert result.exit_code == 1 and "private value" not in result.output
    record = (project.logs / "cli.jsonl").read_text(encoding="utf-8")
    assert json.loads(record.splitlines()[-1])["failure"]["exception_type"] == "PermissionError"
    assert "private value" not in record


def test_cli_draw_freezes_the_scene_and_waits_for_saved_files(project, monkeypatch):
    from types import SimpleNamespace

    from komaquill.api import Client
    from komaquill.cli import web

    runner = CliRunner()
    prefix = ["--project", str(project.root), "chat"]
    created = runner.invoke(app, [*prefix, "create", "Scene"])
    conversation = json.loads(created.output)
    assert conversation["definition"]["llm_profile"] == "lmstudio-4b"
    assert conversation["definition"]["locale"] == "ja"
    client = Client(project.root)
    captured = []

    def execute(identity):
        captured.append(client.library.store.job(identity)[0])
        return SimpleNamespace(id=identity, image_id=identity)

    client.library.execute = execute
    client.library.job = lambda identity: None
    client.images.wait = lambda identity: SimpleNamespace(run_id=identity, images=["scene.png"])
    monkeypatch.setattr(web, "Client", lambda _: client)
    drawn = runner.invoke(app, [*prefix, "draw", conversation["id"], "--allow-real-llm"])
    assert drawn.exit_code == 0, drawn.output
    assert captured[0].request.expected_revision == 0
    assert captured[0].request.allow_real_llm is True
    assert "scene.png" in drawn.output
    client.library.execute = lambda _: SimpleNamespace(image_id=None, error="generation_failed")
    failed = runner.invoke(app, [*prefix, "draw", conversation["id"]])
    assert failed.exit_code == 1 and "Traceback" not in failed.output
