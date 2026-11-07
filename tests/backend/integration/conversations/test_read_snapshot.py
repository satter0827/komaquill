"""Keep a conversation header and its events in one concurrent read snapshot."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from komaquill.api import Client, ConversationInput, MoveAction, SendInput


def test_reader_does_not_mix_a_previous_header_with_newly_committed_events(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput.model_validate(
            {
                "title": "Snapshot",
                "llm_profile": "fake",
            }
        )
    )
    repository = api.conversations.repository
    selected, proceed = Event(), Event()
    original = repository._conversation

    def pause_after_header(*args, **kwargs):
        selected.set()
        assert proceed.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_conversation", pause_after_header)
    with ThreadPoolExecutor() as executor:
        reading = executor.submit(api.conversations.get, conversation.id)
        assert selected.wait(10)
        try:
            other = Client(tmp_path)
            _, created = other.conversations.accept(
                conversation.id,
                SendInput(
                    operation_id=uuid4(),
                    expected_revision=0,
                    action=MoveAction(destination="Garden"),
                ),
            )
            assert created
        finally:
            proceed.set()
        saved = reading.result(timeout=10)
    assert saved.revision == 0 and saved.actions == []
    latest = other.conversations.get(conversation.id)
    assert latest.revision == 1 and len(latest.actions) == 1
