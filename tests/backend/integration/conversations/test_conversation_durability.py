"""Verify recovery, identity checks and database initialization at storage boundaries."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest
from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.schema import conversations, operations
from komaquill.api import Client, KomaquillError, MoveAction, SendInput
from komaquill.domain.conversations.models import ConversationInput, Reply
from sqlalchemy import select

from tests.backend.support.conversations import accept_action
from tests.backend.support.storage import call_file


def test_validated_reply_survives_restart_without_another_model_call(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Recovery")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    repository = api.conversations.repository
    api.conversations.accept(conversation.id, request)
    assert repository.running(request.operation_id)
    reply = Reply(text="Retained response", state_patch={"character": {"pose": "standing"}})
    repository.checkpoint(request.operation_id, reply)
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
    monkeypatch.setattr(
        api.conversations.responder,
        "reply",
        lambda *_: pytest.fail("Recovery must not call a model"),
    )
    reopened = api.conversations.resume(conversation.id)
    assert reopened.revision == 1 and reopened.turns[0].assistant == reply.text
    assert reopened.state == reply.state_patch.apply(conversation.state)
    saved = api.operations.get(request.operation_id)
    assert saved.state == "complete"
    assert saved.image_state == "disabled"
    assert api.conversations.send(conversation.id, request) == saved
    assert len(api.conversations.resume(conversation.id).turns) == 1


@pytest.mark.parametrize("finished", [True, False])
def test_historical_workflow_only_recovers_its_saved_result(tmp_path, monkeypatch, finished):
    import hashlib

    from langgraph.checkpoint.base import empty_checkpoint

    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Retained action")
    )
    request = SendInput(
        operation_id=uuid4(),
        expected_revision=0,
        text="Continue.",
        action=MoveAction(destination="Garden"),
    )
    api.conversations.accept(scene.id, request)
    repository = api.conversations.repository
    assert repository.running(request.operation_id)
    retained = {
        "operation_id": str(request.operation_id),
        "action_id": str(request.operation_id),
        "images": False,
        "text": "",
    }
    raw = json.dumps(retained)
    with repository.database.engine.begin() as connection:
        connection.execute(
            operations.update().values(
                request=raw,
                fingerprint=hashlib.sha256((str(scene.id) + raw).encode()).hexdigest(),
                owner_started="0",
            )
        )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "version": 1,
        "result": Reply(text="A retained reply", state_patch={}).model_dump(
            mode="json", exclude_unset=True
        )
        if finished
        else None,
    }
    checkpoint["channel_versions"] = {"result": 1}
    repository.database.checkpoints.saver.put(
        {
            "configurable": {
                "thread_id": f"conversation:{request.operation_id}",
                "checkpoint_ns": "",
            }
        },
        checkpoint,
        {"source": "update", "step": 0, "parents": {}},
        {"result": 1},
    )
    monkeypatch.setattr(
        api.conversations.responder, "reply", lambda *_: pytest.fail("Never resend old input")
    )
    restored = api.conversations.resume(scene.id)
    operation = api.operations.get(request.operation_id)
    assert restored.state.place == "Garden" and len(restored.actions) == 1
    if finished:
        assert operation.state == "complete" and restored.turn_count == 1
        assert restored.turns[0].user == "" and restored.turns[0].action == request.action
    else:
        assert operation.state == "interrupted" and not operation.can_resume
        assert operation.error == "operation_conflict" and restored.turn_count == 0
        with pytest.raises(KomaquillError, match="submission_unknown"):
            api.conversations.resume_operation(request.operation_id)
    with repository.database.engine.connect() as connection:
        assert connection.execute(select(operations.c.request)).scalar_one() == raw


def test_commit_failure_retains_reply_checkpoint_for_recovery(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Storage failure")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    repository = api.conversations.repository
    original = repository.commit
    monkeypatch.setattr(
        repository,
        "commit",
        lambda *_: (_ for _ in ()).throw(KomaquillError(code="storage_failed")),
    )
    with pytest.raises(KomaquillError):
        api.conversations.send(conversation.id, request)
    monkeypatch.setattr(repository, "commit", original)
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
    assert len(api.conversations.resume(conversation.id).turns) == 1


@pytest.mark.parametrize(
    "record", ["conversation", "operation_id", "operation_conversation", "request", "slot"]
)
def test_saved_identity_mismatch_is_not_used_or_repaired(tmp_path, record):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Identity")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    api.conversations.accept(conversation.id, request)
    repository = api.conversations.repository
    with repository.database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        if record == "conversation":
            value = conversation.model_copy(update={"id": uuid4()})
            connection.execute(conversations.update().values(payload=value.model_dump_json()))
        elif record == "request":
            connection.execute(
                operations.update().values(
                    request=request.model_copy(update={"text": "changed"}).model_dump_json()
                )
            )
        elif record == "slot":
            connection.execute(operations.update().values(active_slot=None))
        else:
            payload = json.loads(connection.execute(select(operations.c.payload)).scalar_one())
            payload["id" if record == "operation_id" else "conversation_id"] = str(uuid4())
            connection.execute(operations.update().values(payload=json.dumps(payload)))
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.conversations.get(conversation.id) if record == "conversation" else api.operations.get(
            request.operation_id
        )
    if record == "conversation":
        with pytest.raises(KomaquillError, match="storage_failed"):
            api.conversations.list()


def test_concurrent_initialization_and_backup_restore_preserve_saved_data(tmp_path):
    original = Client.initialize(tmp_path / "source")
    conversation = original.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Backup")
    )
    original.conversations.send(
        conversation.id,
        SendInput(
            expected_revision=original.conversations.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Hello",
        ),
    )
    restored = Client.initialize(tmp_path / "restored")
    source = original.workspace / ".artifacts/data/komaquill.sqlite3"
    target = restored.workspace / ".artifacts/data/komaquill.sqlite3"
    with (
        closing(sqlite3.connect(source)) as connection,
        closing(sqlite3.connect(target)) as destination,
    ):
        connection.backup(destination)
    assert (
        restored.conversations.get(conversation.id).model_dump()
        == original.conversations.get(conversation.id).model_dump()
    )
    path = tmp_path / "concurrent # database.sqlite3"
    with ThreadPoolExecutor(max_workers=4) as pool:
        engines = list(pool.map(lambda _: Database(path), range(4)))
    for storage in engines:
        with storage.engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "0012"
            )
        storage.engine.dispose()


def test_state_command_reconciles_a_dead_owner_before_applying(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Dead owner")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    api.conversations.accept(conversation.id, request)
    repository = api.conversations.repository
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
    assert not repository.running(request.operation_id)
    changed, _ = accept_action(
        api.conversations,
        conversation.id,
        SendInput(
            text="Continue.",
            operation_id=uuid4(),
            expected_revision=0,
            action=MoveAction(destination="home"),
        ),
    )
    assert changed.revision == 1
    assert api.operations.get(request.operation_id).state == "interrupted"


def test_configured_client_keeps_one_runtime_snapshot(tmp_path):
    api = Client.initialize(tmp_path).configure()
    frozen = api.images._project.settings()
    (tmp_path / "komaquill.toml").write_text('models=["klein-4b"]', encoding="utf-8")
    assert api.runtime._project.settings() is frozen
    assert api.images._project.settings() is frozen
    assert Client(tmp_path)._project.settings().models == ("klein-4b",)


def test_unknown_process_ownership_does_not_release_the_slot(tmp_path, monkeypatch):
    import psutil
    from komaquill.adapters.persistence import conversations as storage

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Unknown owner")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    api.conversations.accept(conversation.id, request)
    monkeypatch.setattr(
        storage.psutil, "Process", lambda *_: (_ for _ in ()).throw(psutil.AccessDenied())
    )
    with pytest.raises(KomaquillError, match="operation_busy"):
        api.conversations.send(
            conversation.id,
            SendInput(
                expected_revision=api.conversations.overview(conversation.id).revision,
                operation_id=uuid4(),
                text="Hello",
            ),
        )
    assert api.operations.get(request.operation_id).state == "accepted"


def test_corrupt_inactive_operation_does_not_block_another_conversation(tmp_path):
    api = Client.initialize(tmp_path)
    first = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Corrupt history")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    api.conversations.send(first.id, request)
    with api.conversations.repository.database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(operations.update().values(payload="invalid"))
    second = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Healthy")
    )
    assert (
        api.conversations.send(
            second.id,
            SendInput(
                expected_revision=api.conversations.overview(second.id).revision,
                operation_id=uuid4(),
                text="Hello",
            ),
        ).state
        == "complete"
    )
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.operations.get(request.operation_id)


@pytest.mark.parametrize("phase", ["failure", "restart"])
@pytest.mark.parametrize("payload", ["invalid", "[]", '{"status":"response_received"}'])
def test_unreadable_received_call_does_not_block_healthy_conversations(
    tmp_path, monkeypatch, phase, payload
):
    api = Client.initialize(tmp_path)
    first = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Unreadable response")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    api.conversations.accept(first.id, request)
    repository = api.conversations.repository
    assert repository.running(request.operation_id)
    api.library.creator.execution.record(request.operation_id, 0, {"status": "started"})
    path = call_file(tmp_path, request.operation_id)
    path.write_text(payload, encoding="utf-8")
    if phase == "failure":
        result = repository.fail(request.operation_id, "storage_failed")
    else:
        with repository.database.engine.begin() as connection:
            connection.execute(operations.update().values(owner_started="0"))
        api.operations.reconcile()
        result = api.operations.get(request.operation_id)
    assert result.state in {"failed", "interrupted"} and not result.can_resume
    with pytest.raises(KomaquillError, match="submission_unknown"):
        api.conversations.resume_operation(result.id)
    assert path.read_text(encoding="utf-8") == payload
    second = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Healthy")
    )
    assert (
        api.conversations.send(
            second.id,
            SendInput(
                expected_revision=api.conversations.overview(second.id).revision,
                operation_id=uuid4(),
                text="Hello",
            ),
        ).state
        == "complete"
    )
    assert api.conversations.get(first.id).turns == []


def test_unreadable_checkpoint_isolated_without_committing_or_resending(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    first = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Unreadable checkpoint")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    api.conversations.accept(first.id, request)
    repository = api.conversations.repository
    assert repository.running(request.operation_id)
    repository.database.checkpoints.save(f"reply:{request.operation_id}", {"text": 1})
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
    second = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Healthy")
    )
    assert (
        api.conversations.send(
            second.id,
            SendInput(
                expected_revision=api.conversations.overview(second.id).revision,
                operation_id=uuid4(),
                text="Hello",
            ),
        ).state
        == "complete"
    )
    result = api.operations.get(request.operation_id)
    assert (
        result.state == "interrupted" and not result.can_resume and result.error == "storage_failed"
    )
    assert api.conversations.get(first.id).turns == []


def test_failed_migration_rolls_back_ddl_before_retry(tmp_path, monkeypatch):
    from alembic import command

    path = tmp_path / "rollback.sqlite3"
    original = command.upgrade

    def fail(config, revision):
        config.attributes["connection"].exec_driver_sql("CREATE TABLE partial (id INTEGER)")
        raise ValueError("Injected migration failure")

    monkeypatch.setattr(command, "upgrade", fail)
    with pytest.raises(ValueError):
        Database(path)
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )
    monkeypatch.setattr(command, "upgrade", original)
    Database(path).engine.dispose()


@pytest.mark.parametrize("phase", ["prepared", "queued"])
def test_pending_image_recovers_from_its_durable_receipt_while_owner_is_alive(
    tmp_path, session, phase
):
    from komaquill.adapters.persistence.images import save
    from komaquill.adapters.workspace import find_project
    from komaquill.application.images.generation import new_run

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Image receipt")
    )
    request = SendInput(expected_revision=0, operation_id=uuid4(), text="Hello")
    repository = api.conversations.repository
    api.conversations.accept(conversation.id, request)
    assert repository.running(request.operation_id)
    repository.commit(request.operation_id, Reply(text="Saved", state_patch={}))
    from komaquill.adapters.persistence.schema import operations

    historical = repository.operation(request.operation_id).model_copy(
        update={"image_state": "pending"}
    )
    with repository.database.engine.begin() as connection:
        connection.execute(
            operations.update()
            .where(operations.c.id == str(request.operation_id))
            .values(payload=historical.model_dump_json())
        )
    project = find_project(tmp_path)
    run = new_run(project, session, request.operation_id, {})
    run.state = phase
    save(project, run)
    assert api.operations.get(request.operation_id).image_state == (
        "submitted" if phase == "queued" else "pending"
    )


def test_previous_database_upgrade_retains_turns_and_accepts_a_new_operation(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from komaquill.adapters.persistence import conversations as storage

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Upgrade")
    )
    api.conversations.send(
        conversation.id,
        SendInput(
            expected_revision=api.conversations.overview(conversation.id).revision,
            operation_id=uuid4(),
            text="Hello",
        ),
    )
    import sqlite3
    from contextlib import closing

    from komaquill.adapters.persistence.connection import database
    from sqlalchemy import create_engine

    original = api.conversations.get(conversation.id).model_dump_json(
        exclude={"turn_count", "turn_offset", "notes", "source_conversation_id", "source_turn_id"}
    )
    api.close()
    database.cache_clear()
    legacy = tmp_path / "legacy.sqlite3"
    engine = create_engine("sqlite:///" + str(legacy))
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).with_name("migrations")))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0003")
        connection.exec_driver_sql(
            "INSERT INTO conversations(id,payload) VALUES(?,?)", (str(conversation.id), original)
        )
    engine.dispose()
    source = tmp_path / ".artifacts/data/komaquill.sqlite3"
    with closing(sqlite3.connect(legacy)) as before, closing(sqlite3.connect(source)) as after:
        before.backup(after)
    Database(tmp_path / ".artifacts/data/komaquill.sqlite3", migrate=True).engine.dispose()
    api = Client(tmp_path)
    assert len(api.conversations.get(conversation.id).turns) == 1
    assert (
        api.conversations.send(
            conversation.id,
            SendInput(
                expected_revision=api.conversations.overview(conversation.id).revision,
                operation_id=uuid4(),
                text="Hello",
            ),
        ).state
        == "complete"
    )
    assert len(api.conversations.get(conversation.id).turns) == 2
