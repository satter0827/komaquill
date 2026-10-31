"""Verify durable turns, cross-client exclusion and safe HTTP contracts offline."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from komaquill.api import Client, KomaquillError
from komaquill.conversations.models import ConversationInput, SendInput, StateCommand
from komaquill.interfaces.http.app import create_app


@pytest.fixture
def chat(tmp_path):
    return Client.initialize(tmp_path)


def test_turn_is_committed_once_and_reopened(chat):
    conversation = chat.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="日本語の会話")
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    result = chat.conversations.send(conversation.id, request)
    assert result.state == "complete"
    assert chat.conversations.send(conversation.id, request) == result
    reopened = Client(chat.workspace).conversations.get(conversation.id)
    assert len(reopened.turns) == 1 and reopened.revision == 1
    assert reopened.definition.title == "日本語の会話"
    assert reopened.state == {"sample": "greeted"}
    with pytest.raises(KomaquillError, match="operation_conflict"):
        chat.conversations.send(conversation.id, request.model_copy(update={"text": "different"}))


def test_busy_applies_across_clients_but_not_reads(chat):
    conversation = chat.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="First")
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    operation, created = chat.conversations.accept(conversation.id, request)
    assert created and operation.state == "accepted"
    other = Client(chat.workspace)
    assert other.conversations.get(conversation.id).turns == []
    with pytest.raises(KomaquillError, match="operation_busy"):
        other.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
    assert chat.conversations.execute(operation.id).state == "complete"
    assert (
        other.conversations.send(
            conversation.id, SendInput(operation_id=uuid4(), text="Hello")
        ).state
        == "complete"
    )


def test_fake_unknown_input_never_becomes_arbitrary_success(chat):
    conversation = chat.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Fixture")
    )
    result = chat.conversations.send(
        conversation.id, SendInput(operation_id=uuid4(), text="Unknown")
    )
    assert result.state == "failed" and result.error == "fake_input_mismatch"
    assert chat.conversations.get(conversation.id).turns == []


def test_real_model_requires_per_operation_permission(chat):
    conversation = chat.conversations.create(
        ConversationInput(locale="en", title="Real", llm_profile="lmstudio-4b")
    )
    result = chat.conversations.send(conversation.id, SendInput(operation_id=uuid4(), text="Hello"))
    assert result.error == "real_llm_not_allowed"


def test_command_revision_and_reset(chat):
    conversation = chat.conversations.create(
        ConversationInput(
            locale="en", llm_profile="fake", title="State", initial_state={"place": "home"}
        )
    )
    changed = chat.conversations.command(
        conversation.id,
        StateCommand(command="set_state", expected_revision=0, state={"place": "garden"}),
    )
    assert changed.state == {"place": "garden"}
    with pytest.raises(KomaquillError):
        chat.conversations.command(
            conversation.id, StateCommand(command="reset_state", expected_revision=0)
        )
    reset = chat.conversations.command(
        conversation.id, StateCommand(command="reset_state", expected_revision=1)
    )
    assert reset.state == {"place": "home"}


def test_restart_marks_unknown_without_resending(chat):
    from komaquill.adapters.database import operations

    conversation = chat.conversations.create(
        ConversationInput(locale="en", llm_profile="fake", title="Restart")
    )
    request = SendInput(operation_id=uuid4(), text="Hello")
    chat.conversations.accept(conversation.id, request)
    repository = chat.conversations.repository
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
    chat.conversations.resume(conversation.id)
    operation = chat.operations.get(request.operation_id)
    assert operation.state == "interrupted" and operation.error == "submission_unknown"
    assert chat.conversations.send(conversation.id, request).state == "interrupted"
    assert chat.conversations.get(conversation.id).turns == []


def test_http_roundtrip_and_origin_validation(chat):
    with TestClient(create_app(chat)) as http:
        created = http.post(
            "/api/v1/conversations", json={"title": "HTTP", "locale": "en", "llm_profile": "fake"}
        )
        assert created.status_code == 201
        identity = created.json()["id"]
        request = {"operation_id": str(uuid4()), "text": "Hello"}
        sent = http.post(f"/api/v1/conversations/{identity}/turns", json=request)
        assert sent.status_code == 202
        assert (
            http.get(f"/api/v1/operations/{request['operation_id']}").json()["state"] == "complete"
        )
        assert len(http.get(f"/api/v1/conversations/{identity}").json()["turns"]) == 1
        assert (
            http.post(
                "/api/v1/conversations",
                json={"title": "Blocked"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert http.get("/api/v1/missing").status_code == 404
        invalid = http.post("/api/v1/conversations", json={"title": "", "secret": "do-not-echo"})
        assert invalid.status_code == 422 and "do-not-echo" not in invalid.text


def test_contract_export_does_not_create_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "/api/v1/conversations" in create_app().openapi()["paths"]
    assert list(tmp_path.iterdir()) == []


def test_saved_failure_has_an_actionable_http_description(chat):
    with TestClient(create_app(chat)) as http:
        identity = http.post(
            "/api/v1/conversations",
            json={"title": "Failure", "locale": "en", "llm_profile": "fake"},
        ).json()["id"]
        operation_id = str(uuid4())
        http.post(
            f"/api/v1/conversations/{identity}/turns",
            json={"operation_id": operation_id, "text": "Unknown sample"},
        )
        result = http.get(f"/api/v1/operations/{operation_id}").json()
        assert result["error"] == "fake_input_mismatch"
        assert result["error_message"] and result["error"] == "fake_input_mismatch"
