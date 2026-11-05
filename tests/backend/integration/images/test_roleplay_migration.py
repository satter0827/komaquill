"""Preserve legacy text and ambiguous state through an explicit backed-up upgrade."""

import importlib
import json
from uuid import uuid4

import pytest
from komaquill.api import Client, KomaquillError
from sqlalchemy import text


def legacy(tmp_path, *, state=None, corrupt=False):
    api = Client.initialize(tmp_path)
    _ = api.library
    identity = uuid4()
    value = {
        "id": str(identity),
        "created_at": "2026-10-31T00:00:00Z",
        "definition": {
            "title": "Retained story",
            "scenario": "A quiet beach",
            "character": '{"name":"Mira","age":"60","gender":"man"}',
            "user": "A traveler",
            "initial_state": {"place": "shop"},
            "images": False,
        },
        "state": state
        or {"place": "sea", "character_clothing": "black apron", "player_clothing": "red coat"},
        "turns": [
            {
                "operation_id": str(uuid4()),
                "created_at": "2026-10-31T00:00:00Z",
                "user": "Go to the sea",
                "assistant": "Let us go.",
                "narration": "Waves wash over the beach.",
                "state": {"place": "sea"},
                "scene": None,
            }
        ],
        "revision": 1,
    }
    payload = "invalid" if corrupt else json.dumps(value, ensure_ascii=False)
    with api.conversations.repository.database.engine.begin() as connection:
        from tests.backend.support.migrations import legacy_execution

        legacy_execution(connection)
        connection.execute(
            text("INSERT INTO conversations(id,payload) VALUES(:id,:payload)"),
            {"id": str(identity), "payload": payload},
        )
        connection.exec_driver_sql("DROP TABLE command_receipts")
        connection.exec_driver_sql("DROP TABLE state_migration_originals")
        connection.exec_driver_sql("DROP TABLE preset_saves")
        connection.exec_driver_sql("DROP TABLE archived_conversations")
        connection.exec_driver_sql("UPDATE alembic_version SET version_num='0006'")
    from komaquill.adapters.persistence.connection import database

    database.cache_clear()
    return api, identity, payload


def test_structured_old_fields_are_mapped_without_using_prose_and_original_is_retained(
    tmp_path, stopped_database
):
    api, identity, payload = legacy(tmp_path)
    with pytest.raises(KomaquillError, match="migration_required"):
        Client(tmp_path).conversations.list()
    report = api.migrate()
    record = json.loads(report.read_bytes())
    assert record["state"] == "complete" and report.with_name("before.sqlite3").is_file()
    reopened = Client(tmp_path)
    saved = reopened.conversations.get(identity)
    assert saved.state.place == "sea" and saved.state.character.clothing == "black apron"
    assert saved.state.player.clothing == "red coat" and saved.definition.character.age == "60"
    assert saved.definition.scenario.description == "A quiet beach"
    assert saved.turns[0].narration == "Waves wash over the beach."
    with api.conversations.repository.database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT payload FROM archived_conversations WHERE id=:id"),
                {"id": str(identity)},
            ).scalar_one()
            == payload
        )
    assert Client(tmp_path).archive.conversations() == []
    assert Client(tmp_path).archive.conversation(identity).turns[0].assistant == "Let us go."


@pytest.mark.parametrize("corrupt", [False, True])
def test_ambiguous_or_unreadable_state_is_preserved_as_read_only_instead_of_guessed(
    tmp_path, corrupt, stopped_database
):
    api, identity, payload = legacy(tmp_path, state={"unknown_owner": "red coat"}, corrupt=corrupt)
    api.migrate()
    assert Client(tmp_path).conversations.list() == []
    reopened = Client(tmp_path)
    archived = reopened.archive.conversations()
    assert len(archived) == 1 and archived[0].id == identity
    assert archived[0].readable is not corrupt
    if not corrupt:
        assert archived[0].turns[0].user == "Go to the sea"
    with api.conversations.repository.database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        assert (
            connection.execute(
                text("SELECT payload FROM archived_conversations WHERE id=:id"),
                {"id": str(identity)},
            ).scalar_one()
            == payload
        )
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.archive.conversation(uuid4())


def test_explicit_fake_preference_is_not_changed_by_later_state_migration(
    tmp_path, stopped_database
):
    api, _, _ = legacy(tmp_path)
    with api.conversations.repository.database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE preferences SET payload=json_set(payload,'$.llm_profile','fake')"
        )
    api.migrate()
    assert Client(tmp_path).library.preferences().llm_profile == "fake"


def test_migration_rejects_ambiguous_fields_and_never_downgrades_in_place():
    migration = importlib.import_module(
        "komaquill.adapters.persistence.migrations.versions.0007_roleplay_state"
    )
    assert (
        migration.state(
            {"sample": "greeted", "character_location": "sea", "character_pose": "standing"}
        )["place"]
        == "sea"
    )
    with pytest.raises(ValueError):
        migration.state({"place": "shop", "character_location": "sea"})
    with pytest.raises(RuntimeError):
        migration.downgrade()
