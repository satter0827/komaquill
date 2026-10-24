"""Exercise Python-facing use cases without CLI wrappers or provider connections."""

import json
from contextlib import contextmanager
from uuid import uuid4

from komaquill.api import Client, StorySnapshot, services, stories
from komaquill.infrastructure.diagnostics import operation_scope, record_event
from tests.support.stories import source


def test_python_plan_uses_text_without_console_or_gpu(project, monkeypatch, capsys):
    def forbidden(*args):
        raise AssertionError("Planning must not connect to an image backend")

    monkeypatch.setattr(stories, "connected", forbidden)
    client = Client(project.root)
    plan = client.stories.plan(source(), llm_profile="fake")
    snapshot = client.stories.show(plan.request.story_id)
    assert isinstance(snapshot, StorySnapshot)
    assert snapshot.image_count == 4 and not snapshot.complete
    assert client.stories.export(plan.request.story_id)["story"]["source"]["text"] == source()
    assert capsys.readouterr() == ("", "")


def test_python_progress_and_resume_share_saved_results(
    project, session, client, monkeypatch, capsys
):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connect(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connect)
    api = Client(project.root)
    events = list(api.stories.generate(source(), llm_profile="fake", images=1, seed=7))
    final = events[-1].snapshot
    assert final.complete and final.scenes[0].primary_image
    assert api.artifact_path(final.scenes[0].primary_image.key).is_file()
    assert final.render_id
    resumed = list(api.stories.resume(final.render_id))
    assert resumed[-1].snapshot.scenes[0].run_id == final.scenes[0].run_id
    assert capsys.readouterr() == ("", "")


def test_python_image_and_evaluation_services_use_common_records(
    project, session, client, monkeypatch
):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connect(_):
        yield client, active

    monkeypatch.setattr(services, "connected", connect)
    api = Client(project.root)
    run = api.images.generate("a red flower", seed=9)
    assert run.state == "saved"
    assert api.images.collect(run.run_id).errors == ()
    assert api.evaluations.list() == []
    assert api.evaluations.export(run.run_id)["runs"][0]["run"]["run_id"] == str(run.run_id)


def test_client_initialization_is_explicit_and_does_not_overwrite(tmp_path):
    root = tmp_path / "without-git"
    api = Client.initialize(root)
    assert api.workspace == root.resolve()
    assert not (root / ".git").exists()
    settings_file = root / "komaquill.toml"
    settings_file.write_text("poll_interval=2.0\n", encoding="utf-8")
    previous = settings_file.read_bytes()
    Client.initialize(root)
    assert settings_file.read_bytes() == previous


def test_operation_ids_do_not_inherit_a_stopped_session(project):
    project.state.mkdir(parents=True)
    identity = uuid4()
    (project.state / "monitor.json").write_text(
        json.dumps({"operation_id": str(uuid4()), "phase": "stopped", "runtime_id": str(identity)})
    )
    with operation_scope() as operation:
        record_event(project, "story_planning", "started")
        record_event(project, "story_planning", "saved")
    events = [json.loads(s) for s in (project.logs / "cli.jsonl").read_text().splitlines()]
    assert {e["operation_id"] for e in events} == {str(operation)}
    assert all(e["session_operation_id"] is None for e in events)


def test_session_service_does_not_authenticate_interactively_by_default(project, monkeypatch):
    seen = []
    monkeypatch.setattr(services.supervisor, "start", lambda *args, **kwargs: seen.append(kwargs))
    api = Client(project.root)
    monkeypatch.setattr(services.inspection, "inspect", lambda _: "observed")
    assert api.sessions.start() == "observed"
    assert seen == [{"interactive_auth": False}]


def test_import_is_quiet_and_does_not_load_optional_clients(tmp_path):
    import subprocess
    import sys

    script = (
        "import sys; from komaquill.api import Client; "
        "assert not any(x.startswith(('langchain', 'langsmith', 'colab_cli')) "
        "for x in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert list(tmp_path.iterdir()) == []
