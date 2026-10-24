"""Exercise partial progress, recovery and independent rendering variants."""

import json
from uuid import uuid4

import pytest

from komaquill.application.generation import collect
from komaquill.application.stories.rendering import create_render, render_story, story_view
from komaquill.domain.errors import KomaquillError
from tests.support.stories import planned


def bound(project, session):
    return session.model_copy(update={"settings": project.settings()})


def test_progressive_render_and_resume_do_not_duplicate_images(project, session, client, stub):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony", 42)
    snapshots = []
    view = render_story(
        repository, project, client, active, render.render_id, notify=snapshots.append
    )
    assert view["complete"] and stub.submissions == 4
    assert any(item["counts"].get("saved") == 1 for item in snapshots)
    assert [item.seed for item in render.scenes] == [42, 43, 44, 45]
    assert all(scene["primary_image"]["key"].startswith("runs/") for scene in view["scenes"])
    render_story(repository, project, client, active, render.render_id)
    assert stub.submissions == 4
    another = create_render(repository, project, active, plan.request.story_id, "pony", 70)
    render_story(repository, project, client, active, another.render_id)
    assert stub.submissions == 8
    assert story_view(repository, project, plan.request.story_id)["render_id"] == str(
        another.render_id
    )
    assert story_view(repository, project, plan.request.story_id, render.render_id)["complete"]


def test_interrupt_leaves_one_request_for_monitor_and_resume(project, session, client, stub):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")

    def interrupt(view):
        if view["scenes"][0]["state"] == "queued":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        render_story(repository, project, client, active, render.render_id, notify=interrupt)
    assert stub.submissions == 1
    assert collect(project, client, active).saved == 1
    assert story_view(repository, project, plan.request.story_id)["counts"]["saved"] == 1
    assert render_story(repository, project, client, active, render.render_id)["complete"]
    assert stub.submissions == 4


def test_unknown_acceptance_is_recovered_without_resubmission(project, session, client, stub):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    stub.drop_response = True
    with pytest.raises(KomaquillError):
        render_story(repository, project, client, active, render.render_id)
    assert stub.submissions == 1
    stub.drop_response = False
    assert render_story(repository, project, client, active, render.render_id)["complete"]
    assert stub.submissions == 4


def test_failed_scene_does_not_block_others_and_retry_keeps_old_attempt(
    project, session, client, stub, monkeypatch
):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    original = client.submit

    def submit(*args):
        prompt = original(*args)
        if stub.submissions == 2:
            stub.history[prompt]["status"] = {"status_str": "error", "completed": True}
        return prompt

    monkeypatch.setattr(client, "submit", submit)
    view = render_story(repository, project, client, active, render.render_id)
    assert view["counts"] == {"saved": 3, "failed": 1}
    assert not render_story(repository, project, client, active, render.render_id)["complete"]
    assert stub.submissions == 4
    assert render_story(repository, project, client, active, render.render_id, retry_failed=True)[
        "complete"
    ]
    assert stub.submissions == 5
    assert len(repository.render(render.render_id).scenes[1].run_ids) == 2


def test_collection_failure_stops_new_scenes_and_can_be_retried(project, session, client, stub):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    stub.corrupt_image = True
    with pytest.raises(KomaquillError):
        render_story(repository, project, client, active, render.render_id)
    assert stub.submissions == 1
    stub.corrupt_image = False
    assert render_story(repository, project, client, active, render.render_id)["complete"]
    assert stub.submissions == 4


def test_two_renderers_and_changed_conditions_are_rejected(project, session, client):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    with repository.claim(render.render_id), pytest.raises(KomaquillError):
        render_story(repository, project, client, active, render.render_id)
    changed = active.model_copy(
        update={
            "settings": active.settings.model_copy(
                update={
                    "presets": {
                        **active.settings.presets,
                        "pony": active.settings.presets["pony"].model_copy(update={"steps": 1}),
                    }
                }
            )
        }
    )
    with pytest.raises(KomaquillError):
        render_story(repository, project, client, changed, render.render_id)
    with pytest.raises(KomaquillError):
        create_render(repository, project, active, plan.request.story_id, "missing")
    with pytest.raises(KomaquillError):
        repository.render(uuid4())


def test_reserved_request_before_submission_is_resumable_and_link_precedes_http(
    project, session, client, monkeypatch
):
    repository, plan = planned(project, 1)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    reserved = uuid4()
    link = render.scenes[0].model_copy(update={"run_ids": [reserved]})
    repository.save_render(render.model_copy(update={"scenes": [link]}))
    original = client.submit

    def submit(*args):
        assert repository.render(render.render_id).scenes[0].run_ids == [reserved]
        assert project.repository.read(reserved).pending
        return original(*args)

    monkeypatch.setattr(client, "submit", submit)
    assert render_story(repository, project, client, active, render.render_id)["complete"]


def test_wrong_story_reference_and_corrupted_render_are_not_hidden(project, session, client):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    _, other = planned(project)
    with pytest.raises(KomaquillError):
        story_view(repository, project, other.request.story_id, render.render_id)
    file = (
        repository.root
        / str(plan.request.story_id)
        / "renders"
        / str(render.render_id)
        / "render.json"
    )
    data = json.loads(file.read_bytes())
    data["render_id"] = str(uuid4())
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(KomaquillError):
        repository.render(render.render_id)
    with pytest.raises(KomaquillError):
        story_view(repository, project, plan.request.story_id)


def test_session_stop_prevents_new_scene_submission(project, session, client, stub, monkeypatch):
    repository, plan = planned(project)
    active = bound(project, session)
    render = create_render(repository, project, active, plan.request.story_id, "pony")
    monkeypatch.setattr(type(project), "stop_requested", lambda *args: False)
    with pytest.raises(KomaquillError) as error:
        render_story(repository, project, client, active, render.render_id)
    assert error.value.code == "stopping" and stub.submissions == 0
    assert not any(scene.run_ids for scene in repository.render(render.render_id).scenes)
