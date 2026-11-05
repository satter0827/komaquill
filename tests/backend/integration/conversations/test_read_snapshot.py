"""Keep a conversation header and its events in one concurrent read snapshot."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from komaquill.api import Client, ConversationInput, VisualReview


def test_reader_does_not_mix_a_previous_header_with_newly_committed_events(tmp_path, monkeypatch):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput.model_validate(
            {
                "title": "Snapshot",
                "llm_profile": "fake",
                "character": {"visual_review_required": True},
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
            changed = other.conversations.review_visuals(
                conversation.id,
                VisualReview.model_validate(
                    {
                        "operation_id": str(uuid4()),
                        "expected_revision": 0,
                        "character": {
                            "appearance": "Silver hair",
                            "initial_clothing": "Black apron",
                            "current_clothing": "Black apron",
                        },
                    }
                ),
            )
            assert changed.revision == 1
        finally:
            proceed.set()
        saved = reading.result(timeout=10)
    assert saved.revision == 0 and saved.visual_reviews == []
    latest = other.conversations.get(conversation.id)
    assert latest.revision == 1 and len(latest.visual_reviews) == 1
