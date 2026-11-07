"""Exercise source-backed meeting updates through the durable send path."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client, ConversationInput, SceneRequest, SendInput
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from tests.backend.support.conversations import scripted_llm_availability


def reply(change=None, *, query=True, text=""):
    return {
        "player_move": None,
        "response": {
            "kind": "companion",
            "changes": [],
            "action_narration": "",
            "text": text,
            "meeting_change": {"date": None, "time": None, "place": None, **change}
            if change is not None
            else None,
            "meeting_query": "only" if query else "none",
        },
    }


def model_responses(client, monkeypatch, responses):
    name = client._execution.settings.llm_profiles["lmstudio-4b"].model
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(content=json.dumps(value), response_metadata={"model_name": name})
            for value in responses
        ]
    )
    monkeypatch.setattr(client._execution, "_model", lambda _: model)


def test_meeting_updates_are_grounded_and_inherited_at_the_fork(tmp_path, monkeypatch):
    scripted_llm_availability(monkeypatch)
    with Client.initialize(tmp_path) as client:
        scene = client.conversations.create(ConversationInput(title="Reading group", locale="en"))
        first = "We will meet on Friday at 10 AM at Town Hall."
        correction = "Change only the time to 11 AM."
        cancel = "I am cancelling the meeting."
        new = "The new meeting place is Central Station."
        model_responses(
            client,
            monkeypatch,
            [
                reply(
                    {
                        "kind": "set",
                        "quote": first,
                        "date": "Friday",
                        "time": "10 AM",
                        "place": "Town Hall",
                    },
                    text="The meeting is at 3 PM at the wrong place.",
                ),
                reply({"kind": "set", "quote": correction, "time": "11 AM"}),
                reply(),
                reply({"kind": "cancel", "quote": cancel}),
                reply({"kind": "set", "quote": new, "place": "Central Station"}, query=False),
                reply(),
            ],
        )

        def send(identity, text):
            request = SendInput(
                text=text,
                operation_id=uuid4(),
                expected_revision=client.conversations.overview(identity).revision,
                allow_real_llm=True,
            )
            operation = client.conversations.send(identity, request)
            assert operation.state == "complete", operation.error
            assert client.conversations.send(identity, request) == operation
            return client.conversations.get(identity).turns[-1]

        original = send(scene.id, first + " What is the meeting plan?")
        revised = send(scene.id, correction + " What is the meeting plan?")
        conditional = send(
            scene.id, "If it rains, could we meet at the café? Where are we meeting?"
        )
        cancelled = send(scene.id, cancel + " What is its status?")
        replacement = send(scene.id, new + " What is the meeting plan?")

        assert original.assistant == "" and original.meeting.time == "10 AM"
        assert revised.meeting.model_dump() == {
            "status": "confirmed",
            "date": "Friday",
            "time": "11 AM",
            "place": "Town Hall",
        }
        assert conditional.meeting == revised.meeting
        assert cancelled.meeting.status == "cancelled" and cancelled.meeting.time is None
        assert replacement.meeting.place == "Central Station" and replacement.meeting.time is None
        assert revised.meeting_state.place.turn_id == original.operation_id
        assert revised.meeting_state.time.span.read(revised.user) == "11 AM"
        assert "meeting_state" not in revised.model_dump()
        forked = client.conversations.fork(
            scene.id,
            SceneRequest(
                operation_id=uuid4(),
                expected_revision=client.conversations.overview(scene.id).revision,
                turn_id=revised.operation_id,
                name="Before cancellation",
            ),
        )
        fork_answer = send(forked.id, "What is the meeting plan?")
        assert fork_answer.meeting == revised.meeting
        assert len(client.conversations.get(scene.id).turns) == 5


@pytest.mark.parametrize("quote", ["Go to the park.", "Travel to the park."])
def test_missing_or_repeated_move_evidence_leaves_the_scene_unchanged(tmp_path, monkeypatch, quote):
    scripted_llm_availability(monkeypatch)
    with Client.initialize(tmp_path) as client:
        scene = client.conversations.create(
            ConversationInput(title="Reading group", locale="en", initial_state={"place": "Shop"})
        )
        candidate = reply(query=False)
        candidate["player_move"] = {"kind": "move", "destination": "Park", "quote": quote}
        model_responses(client, monkeypatch, [candidate, candidate])
        operation = client.conversations.send(
            scene.id,
            SendInput(
                operation_id=uuid4(),
                expected_revision=0,
                text="Go to the park. Go to the park. What should we read?",
                allow_real_llm=True,
            ),
        )
        assert operation.error == "invalid_response"
        assert not client.conversations.actions(scene.id)
        assert client.conversations.overview(scene.id).state.place == "Shop"
        assert len(client.llm_records(operation.id)) == 2


@pytest.mark.parametrize("destination", ["Shop", "Town Hall"])
def test_meeting_decision_cannot_also_be_applied_as_travel(tmp_path, monkeypatch, destination):
    scripted_llm_availability(monkeypatch)
    with Client.initialize(tmp_path) as client:
        scene = client.conversations.create(
            ConversationInput(title="Reading group", locale="en", initial_state={"place": "Shop"})
        )
        text = f"We will meet on Friday at {destination}."
        meeting = {"kind": "set", "quote": text, "date": "Friday", "place": destination}
        correct = reply(meeting)
        conflict = {
            **correct,
            "player_move": {"kind": "move", "destination": destination, "quote": text},
        }
        model_responses(client, monkeypatch, [conflict, correct])
        operation = client.conversations.send(
            scene.id,
            SendInput(operation_id=uuid4(), expected_revision=0, text=text, allow_real_llm=True),
        )
        assert operation.state == "complete" and len(client.llm_records(operation.id)) == 2
        saved = client.conversations.get(scene.id)
        assert saved.state.place == "Shop" and not saved.actions
        assert saved.turns[0].meeting.place == destination
