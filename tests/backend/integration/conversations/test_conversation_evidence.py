"""Verify memory provenance and preservation of former summaries."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client, ConversationInput, SendInput

from tests.backend.support.storage import sqlite_connection

pytestmark = pytest.mark.usefixtures("scripted_services")


@pytest.mark.parametrize("damage", ["quote", "turn", "field"])
def test_memory_passage_is_checked_against_immutable_source(tmp_path, monkeypatch, damage):
    from tests.backend.integration.conversations.test_conversation_memory import prepare

    api, scene, calls = prepare(tmp_path, monkeypatch)
    first = api.conversations.send(
        scene.id,
        SendInput(
            expected_revision=api.conversations.overview(scene.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert first.state == "complete"
    saved = api.conversations.get(scene.id)
    memory = saved.memory.model_dump(mode="json")
    passage = memory["passages"][0]
    if damage == "quote":
        passage["quote"] = "An invented promise."
    elif damage == "turn":
        passage["turn_id"] = str(uuid4())
    else:
        passage["field"] = "assistant"
    memory["text"] = "\n".join(item["quote"] for item in memory["passages"])
    from sqlalchemy import text

    with api.conversations.repository.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE conversations SET payload=json_set(payload,'$.memory',json(:memory)) "
                "WHERE id=:id"
            ),
            {"memory": json.dumps(memory), "id": str(scene.id)},
        )
    count = len(calls)
    operation = api.conversations.send(
        scene.id,
        SendInput(
            expected_revision=api.conversations.overview(scene.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert operation.error == "storage_failed" and len(calls) == count


def test_source_memory_migration_preserves_original_header_and_turns(tmp_path, stopped_database):

    from komaquill.adapters.persistence.connection import database
    from komaquill.domain.roleplay import ConversationMemory

    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(title="Migration", locale="en", llm_profile="fake")
    )
    api.conversations.send(
        scene.id,
        SendInput(
            allow_real_llm=True,
            expected_revision=api.conversations.overview(scene.id).revision,
            operation_id=uuid4(),
            text="Hello",
        ),
    )
    api.conversations.repository.save_memory(
        scene.id,
        ConversationMemory(
            source_version=4,
            text="An old summary.",
            through_turn=1,
            source_sha256="old hash",
            locale="en",
            model="former-model",
            instructions_sha256="old instructions",
        ),
    )
    path = tmp_path / ".artifacts/data/komaquill.sqlite3"
    api.close()
    api.conversations.repository.database.engine.dispose()
    database.cache_clear()
    with sqlite_connection(path) as connection:
        original = connection.execute("SELECT payload FROM conversations").fetchone()[0]
        turns = connection.execute("SELECT * FROM conversation_turns").fetchall()
        connection.execute(
            "CREATE TABLE content_labels(kind TEXT,id TEXT,rating TEXT,revision INTEGER)"
        )
        connection.execute("UPDATE alembic_version SET version_num='0009'")
    report = api.migrate()
    assert json.loads(report.read_bytes())["state"] == "complete"
    with sqlite_connection(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0012"
        assert (
            connection.execute(
                "SELECT payload FROM execution_migration_originals WHERE kind='conversation_memory'"
            ).fetchone()[0]
            == original
        )
        assert connection.execute("SELECT * FROM conversation_turns").fetchall() == turns
    with Client(tmp_path) as restored:
        assert restored.conversations.get(scene.id).memory is None
        assert restored.conversations.get(scene.id).turn_count == 1


@pytest.mark.parametrize("tamper", [None, "user", "state", "action_narration"])
def test_omitted_historical_defaults_preserve_source_hash_and_memory(tmp_path, monkeypatch, tamper):
    from komaquill.domain.conversations.history import advance_history
    from sqlalchemy import text

    from tests.backend.integration.conversations.test_conversation_memory import prepare

    api, scene, calls = prepare(tmp_path, monkeypatch)
    repository = api.conversations.repository
    retained = []
    prefix = ""
    with repository.database.engine.begin() as connection:
        for position, payload in connection.execute(
            text("SELECT position,payload FROM conversation_turns ORDER BY position")
        ).all():
            value = json.loads(payload)
            for field in ("character_place", "elapsed_minutes", "intention", "scenery"):
                value["state"].pop(field, None)
            prefix = advance_history(prefix, value)
            payload = json.dumps(value, ensure_ascii=False)
            connection.execute(
                text(
                    "UPDATE conversation_turns SET payload=:payload,prefix_sha256=:prefix "
                    "WHERE position=:position"
                ),
                {"payload": payload, "prefix": prefix, "position": position},
            )
            retained.append((payload, prefix))
    assert len(api.conversations.get(scene.id).turns) == len(retained)
    assert repository.history_hash(scene.id, 0) == ""
    operation = api.conversations.send(
        scene.id,
        SendInput(
            expected_revision=api.conversations.overview(scene.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert operation.state == "complete"
    saved = api.conversations.get(scene.id)
    assert saved.memory.source_sha256 == retained[saved.memory.through_turn - 1][1]
    with repository.database.engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT payload,prefix_sha256 FROM conversation_turns ORDER BY position LIMIT 4"
                )
            ).all()
            == retained
        )
    if tamper is None:
        with pytest.raises(Exception, match="storage_failed"):
            repository.history_hash(scene.id, 100)
        return
    value = json.loads(retained[0][0])
    if tamper == "state":
        value["state"]["place"] = "Another place"
    else:
        value[tamper] = "Altered historical text."
    with repository.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE conversation_turns SET payload=:payload WHERE position=0"),
            {"payload": json.dumps(value)},
        )
    count = len(calls)
    operation = api.conversations.send(
        scene.id,
        SendInput(
            expected_revision=api.conversations.overview(scene.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert operation.error == "storage_failed" and len(calls) == count
