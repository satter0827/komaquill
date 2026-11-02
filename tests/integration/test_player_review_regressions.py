"""Review execution ownership, locale defaults and explicit visual confirmation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.api import (
    ActionRequest,
    CharacterInput,
    Client,
    ConversationInput,
    CreationRequest,
    KomaquillError,
    SendInput,
    VisualReview,
    WaitAction,
)
from komaquill.conversations.models import Reply


def responder(api, function):
    api.conversations.responder = SimpleNamespace(
        profiles=api.conversations.responder.profiles, reply=function
    )


def test_action_inherits_english_and_freezes_effective_locale(tmp_path):
    api = Client.initialize(tmp_path)
    c = api.conversations.create(
        ConversationInput(title="English", locale="en", llm_profile="fake")
    )
    seen = []
    responder(
        api, lambda _, request: seen.append(request.locale) or Reply(text="Hello", state_patch={})
    )
    api.conversations.act(
        c.id, ActionRequest(operation_id=uuid4(), expected_revision=0, action=WaitAction(minutes=1))
    )
    assert seen == ["en"]


def test_explicit_unknown_companion_location_is_not_replaced(tmp_path):
    api = Client.initialize(tmp_path)
    c = api.conversations.create(
        ConversationInput(title="Separate", initial_state={"place": "Beach", "character_place": ""})
    )
    assert c.state.character_place == ""


@pytest.mark.parametrize("phase", ["checkpoint", "commit"])
def test_save_failure_reconciles_without_restarting_live_process(tmp_path, monkeypatch, phase):
    api = Client.initialize(tmp_path)
    c = api.conversations.create(ConversationInput(title="Storage", llm_profile="fake"))
    calls = []
    responder(api, lambda *_: calls.append(True) or Reply(text="Saved reply", state_patch={}))
    request = SendInput(operation_id=uuid4(), text="Hello")
    repository = api.conversations.repository
    original = getattr(repository, phase)
    monkeypatch.setattr(
        repository, phase, lambda *_: (_ for _ in ()).throw(KomaquillError(code="storage_failed"))
    )
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.conversations.send(c.id, request)
    monkeypatch.setattr(repository, phase, original)
    result = api.operations.get(request.operation_id)
    assert result.state == ("complete" if phase == "commit" else "interrupted")
    assert len(calls) == 1
    assert api.conversations.send(c.id, request) == result
    assert len(calls) == 1


def test_live_executor_is_not_reclaimed_or_invoked_twice(tmp_path):
    api = Client.initialize(tmp_path)
    c = api.conversations.create(ConversationInput(title="Active", llm_profile="fake"))
    entered, release = Event(), Event()
    calls = []

    def reply(*_):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return Reply(text="One reply", state_patch={})

    responder(api, reply)
    request = SendInput(operation_id=uuid4(), text="Hello")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(api.conversations.send, c.id, request)
        assert entered.wait(10)
        try:
            assert api.operations.get(request.operation_id).state == "running"
            assert api.conversations.execute(request.operation_id).state == "running"
            assert len(calls) == 1
        finally:
            release.set()
        assert running.result().state == "complete"


def test_finished_creator_is_not_kept_running_when_result_save_fails(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    api.library.creator = SimpleNamespace(draft=lambda _: CharacterInput(name="Draft"))
    request = CreationRequest(
        operation_id=uuid4(), kind="character_draft", prompt="Declared fixture"
    )
    api.library.accept(request)
    original = api.library.store.save_job

    def save(job):
        if job.state == "complete":
            raise KomaquillError(code="storage_failed")
        return original(job)

    monkeypatch.setattr(api.library.store, "save_job", save)
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.library.execute(request.operation_id)
    monkeypatch.setattr(api.library.store, "save_job", original)
    assert api.library.job(request.operation_id).state == "interrupted"


def test_live_creator_is_not_reclaimed_or_invoked_twice(tmp_path):
    api = Client.initialize(tmp_path)
    entered, release = Event(), Event()
    calls = []

    def draft(_):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return CharacterInput(name="Draft")

    api.library.creator = SimpleNamespace(draft=draft)
    request = CreationRequest(
        operation_id=uuid4(), kind="character_draft", prompt="Declared fixture"
    )
    api.library.accept(request)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(api.library.execute, request.operation_id)
        assert entered.wait(10)
        try:
            assert api.library.job(request.operation_id).state == "running"
            assert api.library.execute(request.operation_id).state == "running"
            assert len(calls) == 1
        finally:
            release.set()
        assert running.result().state == "complete"


def test_review_cannot_change_a_confirmed_person_and_retains_unknown_clothes(tmp_path):
    api = Client.initialize(tmp_path)
    c = api.conversations.create(
        ConversationInput(
            title="Review",
            user={"appearance": "Brown hair", "initial_clothing": "Red coat"},
            character={"appearance": "Silver hair and apron", "visual_review_required": True},
        )
    )
    review = VisualReview(
        operation_id=uuid4(),
        expected_revision=0,
        character={"appearance": "Silver hair", "initial_clothing": None, "current_clothing": None},
    )
    saved = api.conversations.review_visuals(c.id, review)
    assert saved.definition.user == c.definition.user
    assert saved.state.player == c.state.player
    assert saved.definition.character.initial_clothing is None
    assert saved.state.character.clothing is None
    assert api.conversations.review_visuals(c.id, review) == saved


def test_review_refuses_updates_to_an_already_confirmed_person(tmp_path):
    api = Client.initialize(tmp_path)
    c = api.conversations.create(
        ConversationInput(
            title="Review",
            user={"appearance": "Brown hair"},
            character={"visual_review_required": True},
        )
    )
    person = {"appearance": "Changed", "initial_clothing": None, "current_clothing": None}
    with pytest.raises(KomaquillError, match="invalid_request"):
        api.conversations.review_visuals(
            c.id,
            VisualReview(
                operation_id=uuid4(), expected_revision=0, player=person, character=person
            ),
        )
    assert api.conversations.get(c.id) == c


def test_http_reply_commit_recovers_while_server_process_stays_alive(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from komaquill.interfaces.http.app import create_app

    api = Client.initialize(tmp_path)
    c = api.conversations.create(ConversationInput(title="HTTP", llm_profile="fake"))
    calls = []
    responder(api, lambda *_: calls.append(True) or Reply(text="Saved", state_patch={}))
    repository = api.conversations.repository
    original = repository.commit
    monkeypatch.setattr(
        repository,
        "commit",
        lambda *_: (_ for _ in ()).throw(KomaquillError(code="storage_failed")),
    )
    identity = uuid4()
    with TestClient(create_app(api), raise_server_exceptions=False) as http:
        receipt = http.post(
            f"/api/v1/conversations/{c.id}/turns",
            json={"operation_id": str(identity), "text": "Hello"},
        )
        assert receipt.status_code == 202
        monkeypatch.setattr(repository, "commit", original)
        assert http.get(f"/api/v1/operations/{identity}").json()["state"] == "complete"
        assert len(http.get(f"/api/v1/conversations/{c.id}").json()["turns"]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("role", ["user", "character"])
def test_drawing_cannot_bypass_an_unreviewed_offscreen_person(tmp_path, role):
    api = Client.initialize(tmp_path)
    c = api.conversations.create(
        ConversationInput(
            title="Review",
            initial_state={"place": "Beach", "character_place": "Bookshop"},
            **{role: {"appearance": "Mixed", "visual_review_required": True}},
        )
    )
    with pytest.raises(KomaquillError, match="visual_review_required"):
        api.library.accept(
            CreationRequest(
                operation_id=uuid4(), kind="conversation_image", target_id=c.id, expected_revision=0
            )
        )
