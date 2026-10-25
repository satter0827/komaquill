"""Exercise terminal outcomes and malformed records through the public interfaces."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from keyring.errors import KeyringError

from komaquill.api import Client, KomaquillError, services, stories
from komaquill.domain.errors import failure_info
from komaquill.infrastructure.stories.publication import refresh_saved_stories
from tests.support.stories import source


@pytest.mark.parametrize(
    "payload",
    ["null", "[]", "{}", '{"format_version":1}', '{"format_version":1,"plan":[],"sha256":"bad"}'],
)
def test_corrupt_plan_is_isolated_from_healthy_works(project, payload):
    api = Client(project.root)
    good = api.stories.plan(source(), llm_profile="fake", images=1)
    bad = api.stories.plan(source(), llm_profile="fake", images=1)
    repository = api.stories._repository
    (repository.folder(bad.request.story_id) / "plan.json").write_text(payload)
    with pytest.raises(KomaquillError) as result:
        api.stories.show(bad.request.story_id)
    assert result.value.code == "storage_failed"
    assert result.value.details["story_id"] == str(bad.request.story_id)
    listing = api.stories.list()
    assert [row.story_id for row in listing.stories] == [good.request.story_id]
    assert listing.unreadable_ids == [bad.request.story_id]
    assert api.stories.show(good.request.story_id).image_count == 1


def test_terminal_failure_finishes_events_and_follow(project, session, client, stub, monkeypatch):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    original = client.submit

    def submit(*args):
        prompt = original(*args)
        stub.history[prompt]["status"] = {"status_str": "error", "completed": True}
        return prompt

    monkeypatch.setattr(client, "submit", submit)
    api = Client(project.root)
    events = list(api.stories.generate(source(), images=1, llm_profile="fake"))
    final = events[-1]
    assert final.kind == "finished"
    assert final.snapshot.finished and not final.snapshot.complete
    monkeypatch.setattr(stories.time, "sleep", lambda _: pytest.fail("Terminal work must not wait"))
    assert list(api.stories.follow(final.snapshot.story_id)) == [final.snapshot]


def test_rendered_corrupt_plan_does_not_escape_projection_recovery(project, session):
    from komaquill.application.stories.rendering import create_render

    api = Client(project.root)
    plan = api.stories.plan(source(), llm_profile="fake", images=1)
    active = session.model_copy(update={"settings": project.settings()})
    create_render(api.stories._repository, project, active, plan.request.story_id, "pony")
    path = api.stories._repository.folder(plan.request.story_id) / "plan.json"
    path.write_text('{"format_version":1}')
    assert refresh_saved_stories(project) == (plan.request.story_id,)


def test_credential_failure_is_safe_at_the_public_boundary(project, monkeypatch):
    monkeypatch.setattr(
        stories,
        "planner",
        lambda *a, **k: (_ for _ in ()).throw(KeyringError("private-sentinel")),
    )
    with pytest.raises(KomaquillError) as result:
        Client(project.root).stories.plan(source(), llm_profile="fake")
    assert result.value.code == "authentication_failed"
    assert not result.value.retryable and "private-sentinel" not in str(result.value)


def test_public_status_and_refresh_translate_known_failures(project, monkeypatch):
    api = Client(project.root)
    monkeypatch.setattr(
        services.inspection,
        "inspect",
        lambda _: (_ for _ in ()).throw(PermissionError("private-sentinel")),
    )
    with pytest.raises(KomaquillError) as status:
        api.sessions.status()
    assert status.value.code == "storage_failed"
    with pytest.raises(KomaquillError) as missing:
        api.stories.refresh(uuid4())
    assert missing.value.code == "request_not_found"


def test_diagnostics_preserve_origin_stage_and_use_context_only_as_default():
    explicit = KomaquillError(code="image_invalid", stage="image_validation")
    assert failure_info(explicit, "collection").stage == "image_validation"
    assert failure_info(KomaquillError(), "collection").stage == "collection"


def test_missing_recorded_history_cannot_trigger_a_second_request(
    project, session, client, stub, monkeypatch
):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    api = Client(project.root)
    snapshot = list(api.stories.generate(source(), images=1, llm_profile="fake"))[-1].snapshot
    path = api.artifact_path(f"runs/{snapshot.scenes[0].run_id}/run.json")
    path.unlink()
    with pytest.raises(KomaquillError) as error:
        list(api.stories.resume(snapshot.render_id))
    assert error.value.code == "storage_failed"
    assert stub.submissions == 1


def test_keyring_adapter_never_constructs_a_model_after_credential_failure(project, monkeypatch):
    from komaquill.infrastructure.stories import llm
    from komaquill.infrastructure.stories.configuration import settings

    options = settings(project)
    profile = options.llm_profiles[options.llm_profile].model_copy(
        update={"key_env": None, "credential_ref": "test"}
    )
    options = options.model_copy(update={"llm_profiles": {options.llm_profile: profile}})
    monkeypatch.setattr(
        llm.keyring,
        "get_password",
        lambda *a: (_ for _ in ()).throw(KeyringError("private-sentinel")),
    )
    with pytest.raises(KomaquillError) as result:
        llm.planner(project, options, allow_real_llm=True)
    assert result.value.code == "authentication_failed"
    assert "private-sentinel" not in str(result.value)


def test_finished_event_and_saved_log_are_not_repeated_on_resume(
    project, session, client, monkeypatch
):
    import json

    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    api = Client(project.root)
    events = list(api.stories.generate(source(), images=1, llm_profile="fake"))
    assert sum(event.kind == "finished" for event in events) == 1
    render_id = events[-1].snapshot.render_id
    assert len(list(api.stories.resume(render_id))) == 1
    saved = [
        json.loads(line)
        for path in project.logs.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("operation") == "generation"
        and json.loads(line).get("result") == "saved"
    ]
    assert len(saved) == 1
