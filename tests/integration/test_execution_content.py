"""Exercise durable execution, content switching and immutable scene branches."""

import json
import sqlite3
from uuid import uuid4

import pytest
from komaquill.adapters.llm.context import ContextBudget
from komaquill.adapters.llm.graph import compile_request, run_request
from komaquill.api import Client, ConversationInput, KomaquillError, SendInput
from komaquill.conversations.editing import NotesRequest, SceneRequest
from komaquill.domain.content import ContentUpdate
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from tests.support.conversations import wire_reply


class Result(BaseModel):
    text: str


def test_sqlite_checkpoint_restarts_after_response_without_resending(tmp_path):
    calls = []
    fail = True

    def send(messages, attempt):
        calls.append(attempt)
        return '{"text":"Saved"}'

    def parse(value):
        nonlocal fail
        if fail:
            fail = False
            raise KomaquillError(code="storage_failed")
        return Result.model_validate_json(value)

    def run(saver):
        return run_request(
            [SystemMessage(content="Policy"), HumanMessage(content="Hello")],
            send,
            parse,
            lambda _: "Correct the response.",
            repair_attempts=1,
            budget=ContextBudget(10000, 10000, 100, 100, "{}"),
            graph=compile_request(saver),
            thread_id="retained",
            schema=Result,
        )

    with (
        SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver,
        pytest.raises(KomaquillError, match="storage_failed"),
    ):
        run(saver)
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        assert run(saver).text == "Saved"
        assert run(saver).text == "Saved"
    assert calls == [0]


def switch(api, enabled):
    return api.library.save_preferences(
        api.library.preferences().model_copy(update={"nsfw_enabled": enabled})
    )


def create(api):
    return api.conversations.create(
        ConversationInput(title="Scene", llm_profile="fake", locale="en")
    )


def test_off_hides_content_and_reclassification_is_explicit(tmp_path):
    api = Client.initialize(tmp_path)
    general = create(api)
    switch(api, True)
    adult = create(api)
    request = SendInput(operation_id=uuid4(), text="Hello")
    operation, _ = api.conversations.accept(adult.id, request)
    assert operation.nsfw_enabled
    switch(api, False)
    assert api.conversations.execute(operation.id).state == "complete"
    assert [item.id for item in api.conversations.list()] == [general.id]
    for read in (api.conversations.get, api.conversations.overview, api.conversations.turns):
        with pytest.raises(KomaquillError, match="content_hidden"):
            read(adult.id)
    switch(api, True)
    label = next(item for item in api.content.list() if item.id == adult.id)
    update = ContentUpdate(
        operation_id=uuid4(),
        id=adult.id,
        kind="conversation",
        rating="general",
        expected_revision=label.revision,
    )
    assert api.content.update(update).rating == "general"
    assert api.content.update(update).rating == "general"
    switch(api, False)
    assert api.conversations.get(adult.id).turn_count == 1


def test_pinned_notes_bookmarks_and_branches_do_not_generate(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    scene = create(api)
    scene = api.conversations.save_notes(
        scene.id,
        NotesRequest(operation_id=uuid4(), expected_revision=0, notes=["Remember the promise."]),
    )
    api.conversations.send(scene.id, SendInput(operation_id=uuid4(), text="Hello"))
    original = api.conversations.get(scene.id)
    turn = original.turns[0]
    assert turn.notes == original.notes
    monkeypatch.setattr(
        api.conversations.responder, "reply", lambda *_: pytest.fail("No generation")
    )
    request = SceneRequest(
        operation_id=uuid4(),
        expected_revision=original.revision,
        turn_id=turn.operation_id,
        name="Branch",
    )
    bookmark = api.conversations.bookmark(scene.id, request)
    assert api.conversations.bookmark(scene.id, request) == bookmark
    branch_request = request.model_copy(update={"operation_id": uuid4()})
    branch = api.conversations.fork(scene.id, branch_request)
    assert api.conversations.fork(scene.id, branch_request).id == branch.id
    assert branch.state == turn.state and branch.notes == turn.notes
    assert branch.source_turn_id == turn.operation_id and branch.id != scene.id
    assert api.conversations.get(scene.id) == original
    with sqlite3.connect(tmp_path / ".artifacts/data/komaquill.sqlite3") as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM conversations WHERE id=?", (str(scene.id),)
            ).fetchone()[0]
        )
        assert "turns" not in payload
    page = api.conversations.turns(branch.id, limit=1)
    assert page["items"] == [turn] and page["next_before"] is None


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_history_rejects_invalid_pages(tmp_path, limit):
    api = Client.initialize(tmp_path)
    with pytest.raises(KomaquillError, match="invalid_request"):
        api.conversations.turns(create(api).id, limit=limit)


def test_completed_parent_recovers_when_final_result_checkpoint_fails(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    scene = create(api)
    repository = api.conversations.repository
    request = SendInput(operation_id=uuid4(), text="Hello")

    def fail(*_):
        raise KomaquillError(code="storage_failed")

    monkeypatch.setattr(repository, "checkpoint", fail)
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.conversations.send(scene.id, request)
    monkeypatch.setattr(
        api.conversations.responder, "reply", lambda *_: pytest.fail("No generation")
    )
    assert api.operations.get(request.operation_id).state == "complete"
    assert len(api.conversations.get(scene.id).turns) == 1
    assert api.operations.get(request.operation_id).state == "complete"


@pytest.mark.parametrize("damage", ["missing", "payload", "identity", "prefix"])
def test_normalized_history_rejects_missing_or_changed_records(tmp_path, damage):
    api = Client.initialize(tmp_path)
    scene = create(api)
    api.conversations.send(scene.id, SendInput(operation_id=uuid4(), text="Hello"))
    with sqlite3.connect(tmp_path / ".artifacts/data/komaquill.sqlite3") as connection:
        if damage == "missing":
            connection.execute("DELETE FROM conversation_turns")
        elif damage == "payload":
            connection.execute(
                "UPDATE conversation_turns SET payload=json_set(payload,'$.assistant','Changed')"
            )
        else:
            connection.execute(
                "UPDATE conversation_turns SET "
                + ("turn_id" if damage == "identity" else "prefix_sha256")
                + "='changed'"
            )
    for read in (api.conversations.get, api.conversations.turns):
        with pytest.raises(KomaquillError, match="storage_failed"):
            read(scene.id)


def test_content_updates_require_target_revision_and_unique_command(tmp_path):
    api = Client.initialize(tmp_path)
    scene = create(api)
    switch(api, True)
    request = ContentUpdate(
        operation_id=uuid4(), kind="conversation", id=scene.id, rating="nsfw", expected_revision=0
    )
    assert api.content.update(request).rating == "nsfw"
    for update in (
        request.model_copy(update={"rating": "general"}),
        request.model_copy(update={"operation_id": uuid4(), "expected_revision": 0}),
    ):
        with pytest.raises(KomaquillError, match="operation_conflict"):
            api.content.update(update)
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.content.update(request.model_copy(update={"id": uuid4(), "operation_id": uuid4()}))
    switch(api, False)
    with pytest.raises(KomaquillError, match="content_hidden"):
        api.content.update(request)


def test_notes_cannot_bypass_revision_busy_or_content_policy(tmp_path):
    api = Client.initialize(tmp_path)
    scene = create(api)
    request = NotesRequest(
        operation_id=uuid4(), expected_revision=0, notes=["Physical facts stay authoritative."]
    )
    operation, _ = api.conversations.accept(scene.id, SendInput(operation_id=uuid4(), text="Hello"))
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.conversations.save_notes(scene.id, request)
    api.conversations.execute(operation.id)
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.conversations.save_notes(scene.id, request)
    switch(api, True)
    request = request.model_copy(update={"expected_revision": 1})
    api.conversations.save_notes(scene.id, request)
    switch(api, False)
    with pytest.raises(KomaquillError, match="content_hidden"):
        api.conversations.save_notes(scene.id, request)


def test_export_copies_strip_image_metadata_and_preserve_originals(tmp_path):
    import hashlib
    import io
    from zipfile import ZipFile

    from komaquill.adapters.conversation_export import export_conversation
    from PIL import Image, PngImagePlugin

    api = Client.initialize(tmp_path)
    scene = create(api)
    api.conversations.send(scene.id, SendInput(operation_id=uuid4(), text="Hello"))
    original = tmp_path / "original.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("prompt", "Private workflow")
    Image.new("RGB", (2, 2), "red").save(original, pnginfo=metadata)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    run = export_run()
    exported = export_conversation(
        tmp_path, api.conversations.get(scene.id), "general", [(run, [original])]
    )
    with ZipFile(exported) as archive:
        assert "Hello" in archive.read("conversation.txt").decode()
        assert not any(name.startswith("conditions/") for name in archive.namelist())
        with Image.open(io.BytesIO(archive.read(f"images/{run.run_id}-0.png"))) as image:
            assert "prompt" not in image.info and image.size == (2, 2)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert not list(exported.parent.glob("*.partial"))


def test_export_failure_does_not_publish_partial_archive(tmp_path):
    from komaquill.adapters.conversation_export import export_conversation

    api = Client.initialize(tmp_path)
    with pytest.raises(FileNotFoundError):
        export_conversation(
            tmp_path, create(api), "general", [(export_run(), [tmp_path / "missing.png"])]
        )
    assert not list((tmp_path / ".artifacts/data/exports").iterdir())


def export_run():
    from komaquill.domain.models import Run

    return Run(
        run_id=uuid4(),
        runtime_id=uuid4(),
        app_version="test",
        source_revision=None,
        remote_source_revision=None,
        comfy_revision=None,
        frontend_version=None,
        workflow_sha256="a" * 64,
    )


def test_distinct_physical_objects_may_have_the_same_name():
    from komaquill.conversations.actions import PrepareAction, apply_action
    from komaquill.domain.roleplay import ConversationState

    state = ConversationState(character={"held_objects": [{"id": "companion-cup", "name": "Cup"}]})
    changed = apply_action(state, PrepareAction(held_objects=[{"id": "player-cup", "name": "Cup"}]))
    assert changed.player.held_objects[0].id == "player-cup"
    with pytest.raises(KomaquillError, match="invalid_request"):
        apply_action(
            state, PrepareAction(held_objects=[{"id": "companion-cup", "name": "Renamed cup"}])
        )
    with pytest.raises(ValueError):
        PrepareAction(held_objects=[{"id": "cup", "name": "Cup"}, {"id": "cup", "name": "Cup"}])


def test_local_execution_reuses_response_and_rejects_changed_configuration(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    api = Client.initialize(tmp_path)
    execution = api.library.creator.execution
    calls = []

    def invoke(*_, **__):
        calls.append(True)
        return AIMessage(
            content='{"text":"Saved"}',
            response_metadata={"model_name": execution.settings.llm_profiles["lmstudio-4b"].model},
        )

    model = SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(execution, "_model", lambda _: SimpleNamespace(bind=lambda **__: model))
    operation = uuid4()

    def run():
        return execution.invoke(
            Result, operation, "lmstudio-4b", True, "Policy", [("user", "Input")]
        )

    assert run().text == "Saved" and run().text == "Saved" and len(calls) == 1
    execution.__dict__["settings"] = execution.settings.model_copy(
        update={"max_output_tokens": execution.settings.max_output_tokens + 1}
    )
    with pytest.raises(KomaquillError, match="operation_conflict"):
        run()
    assert len(calls) == 1


def test_nsfw_execution_rejects_remote_profile_before_model_creation(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    switch(api, True)
    execution = api.library.creator.execution
    local = execution.settings.llm_profiles["lmstudio-4b"]
    execution.__dict__["settings"] = execution.settings.model_copy(
        update={
            "llm_profiles": {
                "remote": local.model_copy(update={"base_url": "https://example.invalid/v1"})
            }
        }
    )
    monkeypatch.setattr(execution, "_model", lambda _: pytest.fail("No remote model"))
    with pytest.raises(KomaquillError, match="invalid_configuration"):
        execution.invoke(Result, uuid4(), "remote", True, "Policy", [("user", "Input")])


def test_explicit_resume_reuses_response_after_validation_storage_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    api = Client.initialize(tmp_path)
    scene = api.conversations.create(ConversationInput(title="Resume", locale="en"))
    execution = api._execution
    calls = []

    def invoke(*_, **__):
        calls.append(True)
        return AIMessage(
            content=json.dumps(
                wire_reply(
                    {
                        "current_state": {
                            "relationship": "",
                            "companion_movement": "stay",
                            "scenery": "A quiet room.",
                            "character": {
                                "clothing": None,
                                "pose": None,
                                "expression": None,
                                "held_objects": None,
                            },
                        },
                        "action_narration": "",
                        "text": "Hello.",
                        "requested_player_action": None,
                    }
                )
            ),
            response_metadata={"model_name": execution.settings.llm_profiles["lmstudio-4b"].model},
        )

    model = SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(execution, "_model", lambda _: SimpleNamespace(bind=lambda **__: model))
    import komaquill.adapters.llm.conversation as responder

    original = responder.check_language

    def fail(*_):
        raise KomaquillError(code="storage_failed")

    monkeypatch.setattr(responder, "check_language", fail)
    request = SendInput(operation_id=uuid4(), text="Hello", allow_real_llm=True)
    result = api.conversations.send(scene.id, request)
    assert result.state == "interrupted" and result.can_resume and len(calls) == 1
    with pytest.raises(KomaquillError, match="real_llm_not_allowed"):
        api.conversations.resume_operation(result.id)
    monkeypatch.setattr(responder, "check_language", original)
    assert api.conversations.resume_operation(result.id, allow_real_llm=True).state == "complete"
    assert len(calls) == 1 and len(api.conversations.get(scene.id).turns) == 1
    assert api.conversations.resume_operation(result.id).state == "complete"


def test_drawing_pair_requires_presence_and_wai(tmp_path, monkeypatch):
    from komaquill.api import CreationRequest
    from komaquill.conversations.context import drawing_context

    from tests.support.conversations import set_state

    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(
            title="Pair", initial_state={"place": "Room", "character_place": "Elsewhere"}
        )
    )
    with pytest.raises(KomaquillError, match="invalid_request"):
        drawing_context(scene, "pair", "observer")
    scene = set_state(api, scene.id, scene.state.model_copy(update={"character_place": "Room"}), 0)
    request = CreationRequest(
        operation_id=uuid4(),
        kind="conversation_image",
        target_id=scene.id,
        expected_revision=scene.revision,
        subjects="pair",
        viewpoint="observer",
    )
    monkeypatch.setattr(api.images, "supports_pair", lambda: False)
    with pytest.raises(KomaquillError, match="image_composition_unavailable"):
        api.library.accept(request)
    monkeypatch.setattr(api.images, "supports_pair", lambda: True)
    job, created = api.library.accept(request)
    assert created and set(json.loads(job.original)["subjects"]) == {"player", "character"}
    assert json.loads(job.original)["viewpoint"] == "observer"


def test_visible_companion_gestures_do_not_require_a_persistent_state_change(tmp_path):
    from komaquill.conversations.models import Reply
    from komaquill.conversations.transitions import published_reply

    api = Client.initialize(tmp_path)
    scene = create(api)
    unsupported = Reply(
        text="Hello.",
        action_narration="The companion briefly nods.",
        state_patch={"character": {"held_objects": []}},
    )
    assert published_reply(scene, unsupported).action_narration == unsupported.action_narration
    supported = Reply(
        text="Hello.",
        action_narration="The companion smiles.",
        state_patch={"character": {"expression": "smiling"}},
    )
    assert published_reply(scene, supported).action_narration == supported.action_narration


def test_operation_polling_and_current_actions_do_not_load_full_history(tmp_path, monkeypatch):
    from komaquill.conversations.actions import ActionRequest, WaitAction

    api = Client.initialize(tmp_path)
    scene = create(api)
    repository = api.conversations.repository
    original = repository._conversation

    def bounded(identity, payload, **options):
        assert options.get("with_turns", True) is False
        return original(identity, payload, **options)

    monkeypatch.setattr(repository, "_conversation", bounded)
    request = ActionRequest(operation_id=uuid4(), expected_revision=0, action=WaitAction(minutes=1))
    action, created = api.conversations.accept_action(scene.id, request)
    assert created and api.conversations.accept_action(scene.id, request) == (action, False)
    assert api.conversations.operations(scene.id)[0].id == action.reaction_id
    assert api.conversations.actions(scene.id) == [action]


def test_action_index_rejects_a_changed_event_identity(tmp_path):
    from komaquill.conversations.actions import ActionRequest, WaitAction

    api = Client.initialize(tmp_path)
    scene = create(api)
    api.conversations.accept_action(
        scene.id,
        ActionRequest(operation_id=uuid4(), expected_revision=0, action=WaitAction(minutes=1)),
    )
    with sqlite3.connect(tmp_path / ".artifacts/data/komaquill.sqlite3") as connection:
        connection.execute("UPDATE conversation_events SET id='changed' WHERE kind='actions'")
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.conversations.actions(scene.id)


@pytest.mark.parametrize("value", ["false", 1, None])
def test_corrupt_content_settings_never_enable_visibility(tmp_path, value):
    api = Client.initialize(tmp_path)
    _ = api.library.preferences()
    with sqlite3.connect(tmp_path / ".artifacts/data/komaquill.sqlite3") as connection:
        payload = json.loads(connection.execute("SELECT payload FROM preferences").fetchone()[0])
        payload["nsfw_enabled"] = value
        connection.execute("UPDATE preferences SET payload=?", (json.dumps(payload),))
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.content.enabled()


def test_optional_graph_dependency_failure_is_reported_explicitly(tmp_path, monkeypatch):
    import sys

    api = Client.initialize(tmp_path)
    scene = create(api)
    monkeypatch.setitem(sys.modules, "komaquill.adapters.llm.workflow", None)
    operation = api.conversations.send(scene.id, SendInput(operation_id=uuid4(), text="Hello"))
    assert operation.state == "failed" and operation.error == "llm_dependencies_missing"


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("archive", ",reason"),
        ("conversation", ""),
        ("preset", ",kind,deleted"),
        ("image", ""),
        ("job", ",owner_pid,owner_started"),
    ],
)
@pytest.mark.parametrize("payload", ["invalid", "[]", '{"definition":null}'])
def test_classification_isolates_unreadable_records_and_updates_only_target(
    tmp_path, monkeypatch, kind, extra, payload
):
    from komaquill.adapters.content import CONTENT_TABLES

    api = Client.initialize(tmp_path)
    healthy = create(api)
    switch(api, True)
    unreadable = uuid4()
    extra_values = {"archive": ",'legacy_state'", "preset": ",'character',0", "job": ",1,'0'"}.get(
        kind, ""
    )
    with sqlite3.connect(tmp_path / ".artifacts/data/komaquill.sqlite3") as connection:
        connection.execute(
            f"INSERT INTO {CONTENT_TABLES[kind]}(id,payload{extra}) VALUES(?,?{extra_values})",
            (str(unreadable), payload),
        )
    labels = api.content.list()
    broken = next(label for label in labels if label.id == unreadable)
    assert not broken.readable
    assert next(label for label in labels if label.id == healthy.id).readable
    monkeypatch.setattr(
        api.content, "list", lambda: pytest.fail("Single-item updates must not read the collection")
    )
    changed = api.content.update(
        ContentUpdate(
            operation_id=uuid4(),
            kind="conversation",
            id=healthy.id,
            rating="nsfw",
            expected_revision=0,
        )
    )
    assert changed.rating == "nsfw" and changed.readable
    assert (
        api.content.update(
            ContentUpdate(
                operation_id=uuid4(),
                kind="conversation",
                id=healthy.id,
                rating="general",
                expected_revision=changed.revision,
            )
        ).rating
        == "general"
    )
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.content.update(
            ContentUpdate(
                operation_id=uuid4(),
                kind=kind,
                id=unreadable,
                rating="general",
                expected_revision=0,
            )
        )
    with sqlite3.connect(tmp_path / ".artifacts/data/komaquill.sqlite3") as connection:
        assert (
            connection.execute(
                f"SELECT payload FROM {CONTENT_TABLES[kind]} WHERE id=?", (str(unreadable),)
            ).fetchone()[0]
            == payload
        )
    switch(api, False)
    assert api.conversations.get(healthy.id).id == healthy.id
