"""Check ordinary CLI output and real-model guards without GPU or LLM calls."""

import json
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from komaquill.api import stories
from komaquill.application.stories.rendering import create_render
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.stories.storage import FileStoryRepository
from komaquill.interfaces.cli import app
from tests.support.stories import planned, source


@pytest.fixture
def cli_story(project, session, client, monkeypatch):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    file = project.root / "story.md"
    file.write_bytes(source().encode())
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(app, ["--project", str(project.root), "stories", *map(str, args)])

    return file, invoke, active


@pytest.mark.parametrize("count", [1, 4, 8])
def test_generate_json_is_single_document_and_progress_is_stderr(project, cli_story, stub, count):
    file, invoke, active = cli_story
    result = invoke(
        "generate", file, "--images", count, "--llm-profile", "fake", "--json", "--seed", 42
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert value["image_count"] == count and value["complete"]
    assert len(value["scenes"]) == count and stub.submissions == count
    assert "Scene" in result.stderr and "Image:" in result.stderr
    assert "\\n" not in result.stderr
    shown = invoke("show", value["story_id"], "--render-id", value["render_id"], "--json")
    assert shown.exit_code == 0 and json.loads(shown.stdout)["complete"]
    resumed = invoke("resume", value["render_id"], "--json")
    assert resumed.exit_code == 0 and stub.submissions == count
    assert json.loads(resumed.stdout)["render_id"] == value["render_id"]
    again = invoke("render", value["story_id"], "--json", "--seed", 43)
    assert again.exit_code == 0 and stub.submissions == count * 2
    assert json.loads(again.stdout)["render_id"] != value["render_id"]


def test_plan_and_show_do_not_connect_or_require_gpu(project, monkeypatch):
    file = project.root / "story.md"
    file.write_bytes(source().encode())

    def forbidden(*args):
        raise AssertionError("Image connection must not be opened.")

    monkeypatch.setattr(stories, "connected", forbidden)
    runner = CliRunner()
    args = ["--project", str(project.root), "stories"]
    result = runner.invoke(app, [*args, "plan", str(file), "--llm-profile", "fake", "--json"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert value["render_id"] is None and value["counts"] == {"unstarted": 4}
    result = runner.invoke(app, [*args, "show", value["story_id"]])
    assert result.exit_code == 0 and "ミオ" in result.output


def test_real_llm_needs_explicit_flag_even_if_keys_are_present(project, monkeypatch):
    file = project.root / "story.md"
    file.write_bytes(source().encode())
    monkeypatch.setenv("KOMAQUILL_LLM_API_KEY", "private-value-must-not-leak")

    def forbidden(*args):
        raise AssertionError("Network must not be opened.")

    monkeypatch.setattr(stories, "connected", forbidden)
    runner = CliRunner()
    for command in ["plan", "generate"]:
        result = runner.invoke(app, ["--project", str(project.root), "stories", command, str(file)])
        assert result.exit_code == 1 and "--allow-real-llm" in result.output
        assert "private-value" not in result.output


def test_generate_refuses_missing_gpu_before_calling_planner(project, cli_story, monkeypatch):
    file, invoke, _ = cli_story

    @contextmanager
    def disconnected(_):
        raise KomaquillError(code="connection_failed")
        yield

    monkeypatch.setattr(stories, "connected", disconnected)
    monkeypatch.setattr(
        stories.StoryService, "_plan", lambda *args: (_ for _ in ()).throw(AssertionError())
    )
    result = invoke("generate", file, "--llm-profile", "fake")
    assert result.exit_code == 1
    assert not FileStoryRepository(project).root.exists()


def test_invalid_image_count_and_encoding_are_actionable(project, cli_story):
    file, invoke, _ = cli_story
    assert invoke("plan", file, "--images", 0, "--llm-profile", "fake").exit_code == 2
    result = invoke("plan", file, "--images", 9, "--llm-profile", "fake")
    assert result.exit_code == 1 and "image count" in result.output.lower()
    file.write_bytes(b"\xff")
    result = invoke("plan", file, "--llm-profile", "fake")
    assert result.exit_code == 1 and "UTF-8" in result.output


def test_interrupt_and_partial_failure_have_clear_exit_status(
    project, cli_story, stub, monkeypatch
):
    file, invoke, _ = cli_story

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    original = stories.render_events
    monkeypatch.setattr(stories, "render_events", interrupt)
    result = invoke("generate", file, "--llm-profile", "fake")
    assert result.exit_code == 130 and "resume" in result.output
    monkeypatch.setattr(stories, "render_events", original)

    def incomplete(repository, workspace, backend, session, render_id, **kwargs):
        render = repository.render(render_id)
        yield stories.story_view(repository, workspace, render.story_id, render_id)

    monkeypatch.setattr(stories, "render_events", incomplete)
    result = invoke("generate", file, "--llm-profile", "fake")
    assert result.exit_code == 1 and "not saved" in result.output


def test_follow_json_changes_are_json_lines_and_do_not_open_viewers(
    project, cli_story, monkeypatch
):
    _, invoke, active = cli_story
    repository, plan = planned(project, 1)
    create_render(repository, project, active, plan.request.story_id, "pony")
    original = stories.story_view
    calls = []

    def changed(*args):
        value = original(*args)
        value["complete"] = len(calls) > 0
        calls.append(value)
        return value

    monkeypatch.setattr(stories, "story_view", changed)
    result = invoke("show", plan.request.story_id, "--follow", "--json")
    assert result.exit_code == 0, result.output
    assert len([json.loads(line) for line in result.stdout.splitlines()]) == 2
    monkeypatch.setattr(
        stories, "story_view", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    assert invoke("show", plan.request.story_id, "--follow").exit_code == 130


def test_planning_keyboard_interrupt_preserves_source_id(project, cli_story, monkeypatch):
    file, invoke, _ = cli_story
    monkeypatch.setattr(
        stories, "plan_story", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    result = invoke("plan", file, "--llm-profile", "fake")
    assert result.exit_code == 130 and "No image requests" in result.output


@pytest.mark.parametrize("devices", [[], [{"type": "cpu"}], "invalid"])
def test_generate_rejects_unavailable_devices_without_planning(
    cli_story, client, monkeypatch, devices
):
    file, invoke, _ = cli_story
    original = client.json
    monkeypatch.setattr(
        client,
        "json",
        lambda route: {"devices": devices} if route == "/system_stats" else original(route),
    )
    monkeypatch.setattr(
        stories.StoryService, "_plan", lambda *args: (_ for _ in ()).throw(AssertionError())
    )
    result = invoke("generate", file, "--llm-profile", "fake")
    assert result.exit_code == 1 and "GPU" in result.output


def test_collection_error_always_identifies_rendering_and_resume_command(cli_story, monkeypatch):
    file, invoke, _ = cli_story

    def failure(*args, **kwargs):
        raise KomaquillError(code="collection_failed")

    monkeypatch.setattr(stories, "render_events", failure)
    result = invoke("generate", file, "--llm-profile", "fake", "--json")
    assert result.exit_code == 1 and result.stdout == ""
    assert "Rendering:" in result.stderr and "stories resume" in result.stderr


def test_planning_diagnostics_keep_safe_failure_classification(project, cli_story):
    file, invoke, _ = cli_story
    file.write_text("Not the paired sample.", encoding="utf-8")
    result = invoke("plan", file, "--llm-profile", "fake")
    assert result.exit_code == 1
    events = [
        json.loads(line)
        for path in project.logs.rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    failed = next(
        row for row in events if row["operation"] == "story_planning" and row["result"] == "failed"
    )
    assert failed["failure"]["code"] == "fake_story_mismatch"
    assert failed["story_id"] and "Not the paired sample." not in json.dumps(events)
