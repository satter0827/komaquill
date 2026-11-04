"""Verify dependency selection, preacceptance rejection and immutable accepted actions."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from komaquill.adapters.llm.conversation import NarrativeReply
from komaquill.api import AvailabilityRequest, CharacterInput, Client, ConversationInput, SendInput
from komaquill.availability import ConnectionObservation
from komaquill.conversations.actions import MoveAction
from komaquill.conversations.models import Reply
from komaquill.domain.errors import KomaquillError
from komaquill.library.models import CreationRequest, PresetSaveOptions
from pydantic import ValidationError


@pytest.mark.parametrize(
    "field,value", [("clothing", []), ("movement", "teleport"), ("held_objects", "cup")]
)
def test_wire_and_parser_reject_the_same_invalid_changes(field, value):
    response = {
        "response": {
            "kind": "companion",
            "changes": [{"field": field, "value": value}],
            "action_narration": "",
            "text": "Hello",
        }
    }
    assert not Draft202012Validator(NarrativeReply.model_json_schema()).is_valid(response)
    with pytest.raises(ValidationError):
        NarrativeReply.model_validate(response, strict=True)


@pytest.mark.parametrize("field", ["relationship", "scenery", "clothing", "expression", "pose"])
@pytest.mark.parametrize("extra", [0, 1])
def test_wire_text_limits_match_the_scene_state(field, extra):
    from komaquill.adapters.llm.conversation import resolve_scene
    from komaquill.conversations.models import Conversation
    from komaquill.domain.roleplay import PersonStatePatch, StatePatch

    model = StatePatch if field in {"relationship", "scenery"} else PersonStatePatch
    limit = next(item.max_length for item in model.model_fields[field].metadata)
    response = {
        "response": {
            "kind": "companion",
            "changes": [{"field": field, "value": "x" * (limit + extra)}],
            "action_narration": "",
            "text": "Hello",
        }
    }
    assert Draft202012Validator(NarrativeReply.model_json_schema()).is_valid(response) == (
        extra == 0
    )
    if extra:
        with pytest.raises(ValidationError):
            NarrativeReply.model_validate(response, strict=True)
    else:
        parsed = NarrativeReply.model_validate(response, strict=True)
        scene = Conversation(id=uuid4(), definition=ConversationInput(title="Limits"))
        state = resolve_scene(scene, parsed.response)
        target = state if model is StatePatch else state.character
        assert getattr(target, field) == "x" * limit


def observed(kind, target, ready=True):
    now = datetime.now(UTC)
    return ConnectionObservation(
        kind=kind,
        target=target,
        state="ready" if ready else "unavailable",
        observed_at=now,
        expires_at=now + timedelta(seconds=15),
        reason=None if ready else "llm_unavailable",
    )


def test_image_dependencies_follow_the_actual_translation_input(tmp_path, monkeypatch):
    client = Client.initialize(tmp_path)
    checks = []
    monkeypatch.setattr(
        client.availability,
        "observe",
        lambda kind, target, **_: checks.append((kind, target)) or observed(kind, target),
    )
    assert client.availability.check(
        AvailabilityRequest(kind="image", prompt="A quiet coast")
    ).ready
    assert checks == [("comfy", "current")]
    checks.clear()
    assert client.availability.check(
        AvailabilityRequest(kind="image", prompt="静かな海岸", llm_profile="fake")
    ).ready
    assert checks == [("comfy", "current"), ("llm", "fake")]
    preset = client.library.save_preset(
        "character",
        CharacterInput(name="Mira", age="60", gender="man"),
        PresetSaveOptions(operation_id=uuid4()),
    ).preset
    checks.clear()
    request = CreationRequest(
        kind="portrait",
        operation_id=uuid4(),
        target_id=preset.id,
        expected_revision=preset.revision,
    )
    client.availability.check(AvailabilityRequest(kind="creation", creation=request))
    assert checks == [("comfy", "current")]


def test_unavailable_send_does_not_persist_action_and_duplicate_does_not_probe(
    tmp_path, monkeypatch
):
    client = Client.initialize(tmp_path)
    service = client.conversations
    scene = service.create(ConversationInput(title="Walk", llm_profile="fake", locale="en"))
    request = SendInput(
        operation_id=uuid4(),
        expected_revision=0,
        text="Let's go.",
        action=MoveAction(destination="Beach"),
    )
    monkeypatch.setattr(
        client.availability, "observe", lambda kind, target, **_: observed(kind, target, False)
    )
    with pytest.raises(KomaquillError, match="llm_unavailable"):
        service.send(scene.id, request)
    assert service.get(scene.id).revision == 0
    assert service.actions(scene.id) == []
    monkeypatch.setattr(
        client.availability, "observe", lambda kind, target, **_: observed(kind, target)
    )
    monkeypatch.setattr(service.responder, "reply", lambda *_: Reply(text="", state_patch={}))
    result = service.send(scene.id, request)
    assert result.state == "complete"
    monkeypatch.setattr(
        client.availability,
        "observe",
        lambda *_, **__: pytest.fail("A retained receipt needs no connection"),
    )
    assert service.send(scene.id, request) == result
    current = service.get(scene.id)
    assert current.turns[-1].user == request.text
    assert current.turns[-1].action == request.action
    assert len(current.actions) == 1
    assert current.actions[0].reaction_id == request.operation_id


@pytest.mark.parametrize("text", ["", "  ", "\n\t"])
def test_an_action_requires_a_nonblank_message(text):
    with pytest.raises(ValidationError):
        SendInput(
            operation_id=uuid4(),
            expected_revision=0,
            text=text,
            action=MoveAction(destination="Beach"),
        )


@pytest.mark.parametrize(
    "loaded,matching,context",
    [(True, True, 8192), (False, True, 8192), (True, False, 8192), (True, True, 0)],
)
def test_native_probe_checks_loaded_identity_without_inference(
    tmp_path, monkeypatch, loaded, matching, context
):
    import httpx
    from komaquill.api import availability as module

    monkeypatch.setenv(
        "KOMAQUILL_CONVERSATION__LLM_PROFILES__LMSTUDIO-4B__TOKEN_COUNTING", "lmstudio"
    )
    client = Client.initialize(tmp_path)
    model = client._execution.settings.llm_profiles["lmstudio-4b"].model
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/api/v1/models"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "type": "llm",
                        "loaded_instances": [
                            {
                                "id": model if matching else "different",
                                "config": {"context_length": context},
                            }
                        ]
                        if loaded
                        else [],
                    }
                ]
            },
        )

    original = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    result = client.availability.observe("llm", "lmstudio-4b")
    assert (result.state == "ready") == (loaded and matching and context > 0)
    assert len(requests) == 1
    assert client.availability.observe("llm", "lmstudio-4b") == result
    assert len(requests) == 1
    client.availability.observe("llm", "lmstudio-4b", refresh=True)
    assert len(requests) == 2


def test_probe_failure_is_safe_and_expired_observations_are_not_reused(tmp_path, monkeypatch):
    import httpx

    client = Client.initialize(tmp_path)
    checks = []

    def unavailable(*_):
        checks.append(True)
        raise httpx.ConnectError("private connection details")

    monkeypatch.setattr(client.availability, "_llm", unavailable)
    first = client.availability.observe("llm", "lmstudio-4b")
    assert first.state == "unavailable" and first.reason == "llm_unavailable"
    assert "private" not in first.model_dump_json()
    expired = first.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    from dataclasses import replace

    key = ("llm", "lmstudio-4b")
    client.availability._observations[key] = replace(
        client.availability._observations[key], result=expired
    )
    client.availability.observe("llm", "lmstudio-4b")
    assert len(checks) == 2


def test_saved_collection_and_local_resume_have_no_connection_dependency(tmp_path, monkeypatch):
    from types import SimpleNamespace

    client = Client.initialize(tmp_path)
    monkeypatch.setattr(client.images, "read", lambda _: SimpleNamespace(state="saved"))
    monkeypatch.setattr(
        client.availability,
        "observe",
        lambda *_a, **_k: pytest.fail("Local recovery needs no connection"),
    )
    assert (
        client.availability.check(
            AvailabilityRequest(kind="collection", identity=uuid4())
        ).connections
        == []
    )
    assert client.images.collect(uuid4()).saved == 1
    monkeypatch.setattr(
        client.conversations.repository,
        "operation_snapshot",
        lambda _: SimpleNamespace(can_resume=True),
    )
    assert client.availability.check(AvailabilityRequest(kind="resume", identity=uuid4())).ready


def test_fixed_content_migration_preserves_history_policy_and_artifacts(tmp_path):
    import hashlib
    import json
    import sqlite3

    from komaquill.adapters.database import database

    client = Client.initialize(tmp_path)
    scene = client.conversations.create(
        ConversationInput(title="Retained", llm_profile="fake", locale="en")
    )
    client.conversations.send(
        scene.id, SendInput(operation_id=uuid4(), expected_revision=0, text="Hello")
    )
    client.library.preferences()
    path = tmp_path / ".artifacts/data/komaquill.sqlite3"
    artifact = path.parent / "runs/retained/conditions.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'{"seed":17,"prompt":"Retained source"}')
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE content_labels(kind TEXT,id TEXT,rating TEXT,revision INTEGER)"
        )
        connection.execute(
            "INSERT INTO content_labels VALUES('conversation',?,'general',0)", (str(scene.id),)
        )
        connection.execute("UPDATE preferences SET payload=json_set(payload,'$.nsfw_enabled',0)")
        connection.execute("UPDATE alembic_version SET version_num='0010'")
        original = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "conversation_turns",
                "conversation_events",
                "operations",
                "operation_policies",
            )
        }
    client.close()
    database.cache_clear()
    record = json.loads(Client(tmp_path).migrate().read_bytes())
    assert record["state"] == "complete" and record["files_before"] == record["files_after"]
    assert (
        record["files_after"]["runs/retained/conditions.json"]
        == hashlib.sha256(artifact.read_bytes()).hexdigest()
    )
    with sqlite3.connect(path) as connection:
        assert all(
            connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table, rows in original.items()
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='content_labels'"
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT payload FROM execution_migration_originals WHERE kind='preferences'"
        ).fetchone()
    reopened = Client(tmp_path)
    assert reopened.conversations.get(scene.id).turn_count == 1
    assert "nsfw_enabled" not in reopened.library.preferences().model_dump()
