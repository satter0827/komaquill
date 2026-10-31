"""Verify recovery, identity checks and database initialization at storage boundaries."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest
from komaquill.adapters.database import Database, conversations, operations
from komaquill.api import Client, KomaquillError
from komaquill.conversations.models import ConversationInput, Reply, SendInput, StateCommand
from sqlalchemy import select


@pytest.mark.parametrize("images", [False, True])
def test_validated_reply_survives_restart_without_another_model_call(tmp_path, monkeypatch, images):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Recovery", images=images)
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    repository = api.conversations.repository
    api.conversations.accept(conversation.id, request)
    assert repository.running(request.operation_id)
    reply = Reply(
        text="Retained response", state={"place": "home"}, scene="a quiet room" if images else None
    )
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
    assert reopened.state == reply.state
    saved = api.operations.get(request.operation_id)
    assert saved.state == "complete"
    assert saved.image_state == "disabled"
    assert api.conversations.send(conversation.id, request) == saved
    assert len(api.conversations.resume(conversation.id).turns) == 1


def test_commit_failure_retains_reply_checkpoint_for_recovery(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Storage failure")
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
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
    request = SendInput(operation_id=uuid4(), text="Hello")
    api.conversations.accept(conversation.id, request)
    repository = api.conversations.repository
    with repository.database.engine.begin() as connection:
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
    original.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
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
                == "0006"
            )
        storage.engine.dispose()


def test_state_command_reconciles_a_dead_owner_before_applying(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Dead owner")
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    api.conversations.accept(conversation.id, request)
    repository = api.conversations.repository
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
    assert not repository.running(request.operation_id)
    changed = api.conversations.command(
        conversation.id,
        StateCommand(command="set_state", expected_revision=0, state={"place": "home"}),
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
    from komaquill.adapters import database as storage

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Unknown owner")
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    api.conversations.accept(conversation.id, request)
    monkeypatch.setattr(
        storage.psutil, "Process", lambda *_: (_ for _ in ()).throw(psutil.AccessDenied())
    )
    with pytest.raises(KomaquillError, match="operation_busy"):
        api.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
    assert api.operations.get(request.operation_id).state == "accepted"


def test_corrupt_inactive_operation_does_not_block_another_conversation(tmp_path):
    api = Client.initialize(tmp_path)
    first = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Corrupt history")
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    api.conversations.send(first.id, request)
    with api.conversations.repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(payload="invalid"))
    second = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Healthy")
    )
    assert (
        api.conversations.send(second.id, SendInput(operation_id=uuid4(), text="Hello")).state
        == "complete"
    )
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.operations.get(request.operation_id)


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
    from komaquill.adapters.image_repository import save
    from komaquill.adapters.workspace import find_project
    from komaquill.images.generation import new_run

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Image receipt", images=True)
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    repository = api.conversations.repository
    api.conversations.accept(conversation.id, request)
    assert repository.running(request.operation_id)
    repository.commit(request.operation_id, Reply(text="Saved", state={}, scene="a room"))
    from komaquill.adapters.database import operations

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
    from komaquill.adapters import database as storage

    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Upgrade")
    )
    api.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
    engine = api.conversations.repository.database.engine
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).with_name("migrations")))
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        config.attributes["connection"] = connection
        connection.exec_driver_sql("DROP INDEX one_active_player")
        command.stamp(config, "0005")
        command.downgrade(config, "0003")
    Database(tmp_path / ".artifacts/data/komaquill.sqlite3", migrate=True).engine.dispose()
    assert len(api.conversations.get(conversation.id).turns) == 1
    assert (
        api.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello")).state
        == "complete"
    )
    assert len(api.conversations.get(conversation.id).turns) == 2
