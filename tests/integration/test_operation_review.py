"""Exercise acceptance and recovery boundaries against the original operation requirements."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from komaquill.adapters.database import operations
from komaquill.api import AvailabilityRequest, Client, ConversationInput, CreationRequest, SendInput
from komaquill.conversations.editing import NotesRequest
from komaquill.conversations.models import Reply
from komaquill.domain.errors import KomaquillError
from komaquill.interfaces.http.app import create_app
from sqlalchemy import func, select


def test_http_connection_rejection_retains_its_safe_failure_stage(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(ConversationInput(title="Connection rejection"))

    def reject(*_args, **_kwargs):
        raise KomaquillError(code="identity_mismatch")

    monkeypatch.setattr(api.availability, "_llm", reject)
    identity = uuid4()
    with TestClient(create_app(api)) as http:
        response = http.post(
            f"/api/v1/conversations/{scene.id}/turns",
            json={
                "operation_id": str(identity),
                "expected_revision": 0,
                "text": "Keep this draft",
                "allow_real_llm": True,
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "identity_mismatch"
        assert response.json()["stage"] == "connection"
        with pytest.raises(KomaquillError, match="request_not_found"):
            api.operations.get(identity)
        assert api.conversations.get(scene.id).revision == 0


def test_resume_availability_does_not_commit_a_retained_reply(tmp_path):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(ConversationInput(title="Read only", llm_profile="fake"))
    identity = uuid4()
    api.conversations.accept(
        scene.id, SendInput(operation_id=identity, expected_revision=0, text="Hello")
    )
    repository = api.conversations.repository
    assert repository.running(identity)
    repository.checkpoint(identity, Reply(text="A saved response", state_patch={}))
    with repository.database.engine.begin() as connection:
        connection.execute(operations.update().values(owner_started="0"))
        before = connection.execute(select(operations.c.payload)).scalar_one()
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.availability.check(AvailabilityRequest(kind="resume", identity=identity))
    assert api.conversations.get(scene.id).turn_count == 0
    with repository.database.engine.connect() as connection:
        assert connection.execute(select(operations.c.payload)).scalar_one() == before
    api.operations.reconcile()
    assert api.operations.get(identity).state == "complete"
    assert api.conversations.get(scene.id).turn_count == 1


def test_completed_operation_metadata_does_not_duplicate_paged_turn_text(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(
        ConversationInput(title="Metadata", llm_profile="fake", locale="en")
    )
    operation = api.conversations.send(
        scene.id, SendInput(operation_id=uuid4(), expected_revision=0, text="Hello")
    )
    assert operation.state == "complete"
    monkeypatch.setattr(
        api.operations, "input", lambda *_: pytest.fail("Completed text belongs to paged turns")
    )
    with TestClient(create_app(api)) as http:
        assert http.get(f"/api/v1/operations/{operation.id}").json()["input"] is None
        assert http.get(f"/api/v1/conversations/{scene.id}/operations").json()[0]["input"] is None


def test_conversation_drawing_rechecks_revision_after_connection_probe(tmp_path):
    api = Client.initialize(tmp_path)
    scene = api.conversations.create(ConversationInput(title="Current scene", llm_profile="fake"))
    identity = uuid4()

    def changed_during_probe(*_):
        api.conversations.save_notes(
            scene.id,
            NotesRequest(operation_id=uuid4(), expected_revision=0, notes=["A changed scene"]),
        )

    api.library.preflight = changed_during_probe
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.library.accept(
            CreationRequest(
                operation_id=identity,
                kind="conversation_image",
                target_id=scene.id,
                expected_revision=0,
            )
        )
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.library.store.job(identity)


def test_a_delayed_probe_cannot_replace_a_newer_failure(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    api = Client.initialize(tmp_path)
    entered, release, counter_lock = Event(), Event(), Lock()
    calls = 0

    def probe(*_):
        nonlocal calls
        with counter_lock:
            calls += 1
            current = calls
        if current == 1:
            entered.set()
            assert release.wait(5)
        else:
            raise KomaquillError(code="llm_unavailable")

    monkeypatch.setattr(api.availability, "_llm", probe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(api.availability.observe, "llm", "fake", refresh=True)
        try:
            assert entered.wait(5)
            latest = api.availability.observe("llm", "fake", refresh=True)
            assert latest.state == "unavailable"
        finally:
            release.set()
        assert old.result().state == "unavailable"
    assert api.availability.observe("llm", "fake").state == "unavailable"


@pytest.mark.parametrize("kind", ["character_draft", "conversation_image"])
def test_creation_availability_does_not_seed_library_records(tmp_path, monkeypatch, kind):
    from komaquill.adapters.library_repository import preferences, presets

    api = Client.initialize(tmp_path)
    scene = api.conversations.create(ConversationInput(title="Only observe", llm_profile="fake"))
    repository = api.conversations.repository

    def counts():
        with repository.database.engine.connect() as connection:
            return tuple(
                connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (presets, preferences)
            )

    assert counts() == (0, 0)
    monkeypatch.setattr(api.availability, "_llm", lambda *_: None)
    monkeypatch.setattr(api.availability, "_comfy", lambda *_: None)
    request = CreationRequest(
        operation_id=uuid4(),
        kind=kind,
        prompt="An idea",
        llm_profile="fake",
        target_id=scene.id if kind == "conversation_image" else None,
        expected_revision=0 if kind == "conversation_image" else None,
    )
    assert api.availability.check(AvailabilityRequest(kind="creation", creation=request)).ready
    assert counts() == (0, 0)
    assert api.library.preferences().locale == "ja"
    assert all(count > 0 for count in counts())


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_operation_exposes_its_saved_input_without_rewriting_history(tmp_path, legacy):
    import hashlib
    import json

    from komaquill.conversations.actions import MoveAction

    api = Client.initialize(tmp_path)
    scene = api.conversations.create(ConversationInput(title="Retained input", llm_profile="fake"))
    identity = uuid4()
    action = MoveAction(destination="Garden")
    saved = api.conversations.send(
        scene.id,
        SendInput(
            operation_id=identity,
            expected_revision=0,
            text="Keep my message after failure",
            action=action,
        ),
    )
    assert saved.state == "failed" and len(api.conversations.get(scene.id).actions) == 1
    with api.conversations.repository.database.engine.begin() as connection:
        if legacy:
            raw = json.dumps(
                {
                    "operation_id": str(identity),
                    "action_id": str(identity),
                    "text": "",
                    "images": False,
                }
            )
            connection.execute(
                operations.update().values(
                    request=raw,
                    fingerprint=hashlib.sha256((str(scene.id) + raw).encode()).hexdigest(),
                )
            )
        original = connection.execute(select(operations.c.request)).scalar_one()
    with TestClient(create_app(api)) as http:
        result = http.get(f"/api/v1/operations/{identity}")
        assert result.status_code == 200
        assert result.json()["input"] == {
            "text": "" if legacy else "Keep my message after failure",
            "action": action.model_dump(mode="json"),
        }
    with api.conversations.repository.database.engine.connect() as connection:
        assert connection.execute(select(operations.c.request)).scalar_one() == original
    assert api.conversations.get(scene.id).turn_count == 0
