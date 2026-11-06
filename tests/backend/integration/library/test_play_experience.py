"""Verify single-player settings, narration and explicitly requested drawings."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from komaquill.api import Client, CreationRequest, KomaquillError, PlayerInput
from komaquill.domain.conversations.models import Reply, SendInput
from komaquill.domain.roleplay import ConversationState
from komaquill.interfaces.http.app import create_app

from tests.backend.support.conversations import set_state
from tests.backend.support.storage import sqlite_connection

pytestmark = pytest.mark.usefixtures("scripted_services")


def start(api):
    from komaquill.domain.library.models import ConversationStart, PresetRef

    return api.library.start(
        ConversationStart(
            player_revision=api.player.get().revision,
            character=PresetRef(id=api.characters.list()[0].id, revision=0),
            story=PresetRef(id=api.stories.list()[0].id, revision=0),
        )
    )


def test_one_player_roundtrips_attributes_and_freezes_them_in_play(tmp_path):
    api = Client.initialize(tmp_path)
    player = api.player.get()
    saved = api.player.update(
        player.revision, PlayerInput(name="Visitor", age="28", gender="woman")
    )
    conversation = start(api)
    assert conversation.definition.user.age == "28"
    api.player.update(saved.revision, PlayerInput(name="Changed", age="40"))
    assert api.conversations.get(conversation.id).definition == conversation.definition
    assert Client(tmp_path).player.get().name == "Changed"
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.player.update(0, PlayerInput(name="Stale"))
    with TestClient(create_app(api), base_url="http://localhost") as http:
        assert http.get("/api/v1/player").json()["age"] == "40"
        assert http.get("/api/v1/players").status_code == 404


def test_dialogue_persists_narration_without_submitting_an_image(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = start(api)
    api.conversations.responder = SimpleNamespace(
        reply=lambda *_: Reply(
            text="Shall we visit the garden?",
            narration="The shopkeeper puts on a blue coat.",
            state_patch={"character": {"clothing": "blue coat"}, "scenery": "Rain on the windows."},
        )
    )
    request = SendInput(
        expected_revision=0, operation_id=uuid4(), text="Continue", allow_real_llm=True
    )
    assert api.conversations.send(conversation.id, request).state == "complete"
    saved = Client(tmp_path).conversations.get(conversation.id)
    assert saved.turns[0].narration == "Rain on the windows."
    assert saved.state.character.clothing == "blue coat"
    assert api.images.list() == []
    assert api.library.conversation_images(conversation.id) == []
    drawing, _ = api.library.accept(
        CreationRequest(
            operation_id=uuid4(),
            kind="conversation_image",
            target_id=conversation.id,
            expected_revision=saved.revision,
            allow_real_llm=True,
        )
    )
    assert drawing.turn_id == saved.turns[0].operation_id


def test_drawing_freezes_current_state_and_never_resubmits_after_reopening(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = start(api)
    changed = set_state(
        api,
        conversation.id,
        ConversationState.model_validate(
            {"place": "garden", "character": {"clothing": "blue coat"}}
        ),
        0,
    )
    request = CreationRequest(
        operation_id=uuid4(),
        kind="conversation_image",
        target_id=conversation.id,
        expected_revision=changed.revision,
        allow_real_llm=True,
    )
    accepted, created = api.library.accept(request)
    assert created and json.loads(accepted.original)["character"]["clothing"] == "blue coat"
    set_state(
        api, conversation.id, ConversationState.model_validate({"place": "tower"}), changed.revision
    )
    calls = []

    def prepare(text, *_args, **_kwargs):
        calls.append(text)
        return "An adult shopkeeper in a blue coat stands in a garden."

    api.library.image_describer = SimpleNamespace(prepare_image=prepare)
    runs = {}

    def get(identity):
        if identity not in runs:
            raise KomaquillError(code="request_not_found")
        return runs[identity]

    def submit(prompt, *, run_id, parameters, purpose=None):
        runs[run_id] = SimpleNamespace(state="queued", images=[], run_id=run_id)

    api.library.images = SimpleNamespace(submit=submit, get=get)
    assert api.library.execute(accepted.id).state == "submitted"
    assert "garden" in calls[0] and "tower" not in calls[0]
    assert api.library.accept(request)[1] is False
    assert api.library.execute(accepted.id).state == "submitted" and len(calls) == 1
    persisted = Client(tmp_path).library.store.conversation_images(conversation.id)
    assert len(persisted) == 1 and persisted[0].original == accepted.original
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.library.accept(request.model_copy(update={"operation_id": uuid4()}))


def test_explicit_environment_choice_uses_environment_conditions_before_submission(tmp_path):
    from komaquill.api import ConversationInput

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(
            title="Alone",
            initial_state={
                "place": "Beach",
                "time": "Sunset",
            },
        )
    )
    submitted = []
    api.library.images = SimpleNamespace(
        submit=lambda prompt, **options: submitted.append((prompt, options)),
        get=lambda identity: (_ for _ in ()).throw(KomaquillError(code="request_not_found")),
    )
    request = CreationRequest(
        llm_profile="fake",
        operation_id=uuid4(),
        kind="conversation_image",
        target_id=conversation.id,
        expected_revision=0,
        subjects="environment",
    )
    job, _ = api.library.accept(request)
    assert api.library.execute(job.id).state == "submitted"
    prompt, options = submitted[0]
    assert options["purpose"] == "banner"
    assert prompt.startswith("Unoccupied setting, environment only.")
    assert "Bookshop" not in prompt and "Beach" in prompt


@pytest.mark.parametrize("choice", ["selected", "unique", "ambiguous", "busy"])
def test_profile_migration_preserves_history_and_refuses_ambiguous_or_pending_work(
    tmp_path, choice, stopped_database
):

    from komaquill.adapters.persistence import migrate as database_migration

    api = Client.initialize(tmp_path)
    conversation = start(api)
    profile = api.player.get()
    if choice == "busy":
        api.conversations.accept(
            conversation.id,
            SendInput(
                allow_real_llm=True,
                expected_revision=api.conversations.overview(conversation.id).revision,
                operation_id=uuid4(),
                text="Pending",
            ),
        )
    source = tmp_path / ".artifacts/data/komaquill.sqlite3"
    from tests.backend.support.migrations import legacy_execution

    with api.conversations.repository.database.engine.begin() as connection:
        legacy_execution(connection)
    with sqlite_connection(source) as db:
        db.execute("DROP TABLE command_receipts")
        db.execute("DROP TABLE state_migration_originals")
        db.execute("DROP TABLE preset_saves")
        db.execute("DROP TABLE archived_conversations")
        db.execute("DROP INDEX one_active_player")
        db.execute("UPDATE alembic_version SET version_num='0005'")
        preferences = json.loads(db.execute("SELECT payload FROM preferences").fetchone()[0])
        preferences.update(
            player_id=str(profile.id) if choice == "selected" else None,
            llm_profile="fake",
            image_frequency="always",
        )
        db.execute("UPDATE preferences SET payload=?", (json.dumps(preferences),))
        if choice in {"selected", "ambiguous"}:
            other = profile.model_copy(update={"id": uuid4(), "name": "Other"})
            db.execute(
                "INSERT INTO presets(id,kind,payload,deleted) VALUES(?,?,?,0)",
                (str(other.id), "player", other.model_dump_json()),
            )
        db.commit()
        before = database_migration.inventory(db)
    if choice in {"ambiguous", "busy"}:
        with pytest.raises(
            KomaquillError, match="operation_busy" if choice == "busy" else "storage_failed"
        ):
            api.migrate()
        with sqlite_connection(source) as db:
            assert database_migration.inventory(db) == before
            assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0005"
        return
    report = api.migrate()
    record = json.loads(report.read_bytes())
    assert record["state"] == "complete"
    reopened = Client(tmp_path)
    assert reopened.player.get().name == profile.name
    assert reopened.player.get().visual_review_required == bool(profile.appearance)
    assert reopened.conversations.get(conversation.id).turns == conversation.turns
    assert reopened.library.preferences().llm_profile == "lmstudio-4b"
    assert report.with_name("before.sqlite3").is_file()


def test_repeated_migration_retains_explicit_current_preferences(tmp_path, stopped_database):
    import sqlite3

    from komaquill.adapters.persistence import migrate as database_migration

    api = Client.initialize(tmp_path)
    api.library.save_preferences(
        api.library.preferences().model_copy(update={"llm_profile": "fake"})
    )
    report = api.migrate()
    record = json.loads(report.read_bytes())
    assert record["before"] == record["after"]
    assert Client(tmp_path).library.preferences().llm_profile == "fake"
    with (
        sqlite3.connect(report.with_name("before.sqlite3")) as before,
        sqlite3.connect(tmp_path / ".artifacts/data/komaquill.sqlite3") as after,
    ):
        after.execute("UPDATE preferences SET payload='{}'")
        assert database_migration.library_preserved(before, after) is False


@pytest.mark.parametrize("state", ["complete", "failed"])
def test_retained_turn_requests_are_read_without_rewriting_original_metadata(tmp_path, state):
    import hashlib

    from komaquill.api import ConversationInput

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(title="Kept", llm_profile="fake", locale="en")
    )
    request = SendInput(
        allow_real_llm=True,
        expected_revision=0,
        operation_id=uuid4(),
        text="Hello" if state == "complete" else "Unknown",
    )
    operation = api.conversations.send(conversation.id, request)
    source = tmp_path / ".artifacts/data/komaquill.sqlite3"
    raw = json.dumps(
        {**request.model_dump(mode="json"), "images": True, "response_length": "detailed"}
    )
    with sqlite_connection(source) as db:
        db.execute(
            "UPDATE operations SET request=?, fingerprint=? WHERE id=?",
            (
                raw,
                hashlib.sha256((str(conversation.id) + raw).encode()).hexdigest(),
                str(operation.id),
            ),
        )
        db.commit()
    assert api.conversations.operations(conversation.id) == [operation]
    assert api.conversations.send(conversation.id, request) == operation
    with sqlite_connection(source) as db:
        assert db.execute("SELECT request FROM operations").fetchone()[0] == raw


def test_drawing_uses_canonical_state_instead_of_conflicting_narration(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = start(api)
    api.conversations.responder = SimpleNamespace(
        reply=lambda *_: Reply(
            text="Hello",
            narration="The woman wears a white shirt in a shop.",
            state_patch={
                "character": {"clothing": "blue shirt", "pose": "sitting"},
            },
        )
    )
    api.conversations.send(
        conversation.id,
        SendInput(
            expected_revision=api.conversations.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    current = api.conversations.get(conversation.id)
    request = CreationRequest(
        operation_id=uuid4(),
        kind="conversation_image",
        target_id=current.id,
        expected_revision=current.revision,
        allow_real_llm=True,
    )
    captured, _ = api.library.accept(request)
    calls = []

    def prepare(text, *_args, **kwargs):
        calls.append((text, kwargs))
        return "A woman in a blue shirt sits in the garden."

    api.library.image_describer = SimpleNamespace(prepare_image=prepare)
    api.library.images = SimpleNamespace(
        submit=lambda *_a, **_kw: None,
        get=lambda identity: SimpleNamespace(state="queued", images=[], run_id=identity),
    )
    assert api.library.execute(captured.id).state == "submitted"
    assert "blue shirt" in calls[0][0] and "white shirt" not in calls[0][0]
    assert '"player"' not in calls[0][0]
    assert calls[0][1]["purpose"] == "conversation_image"
    assert json.loads(captured.original)["character"]["clothing"] == "blue shirt"
