"""Exercise source-backed meeting updates through the durable send path."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client, ConversationInput, SceneRequest, SendInput
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from tests.backend.support.conversations import scripted_llm_availability


def reply(change=None, *, query=None, text=""):
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
            "meeting_query": query,
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
        inquiry = "What is the meeting plan?"
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
                    query=inquiry,
                    text="The meeting is at 3 PM at the wrong place.",
                ),
                reply(
                    {
                        "kind": "set",
                        "quote": correction,
                        "date": "Friday",
                        "time": "11 AM",
                        "place": "Town Hall",
                    },
                    query=inquiry,
                ),
                reply(query="Where are we meeting?", text="We can discuss the café as a fallback."),
                reply({"kind": "cancel", "quote": cancel}, query="What is its status?"),
                reply({"kind": "set", "quote": new, "place": "Central Station"}, query=inquiry),
                reply(query=inquiry),
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
        assert revised.meeting_state.date.turn_id == original.operation_id
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
        candidate = reply()
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


@pytest.mark.parametrize(
    "text,quote,kind,values",
    [
        (
            "初めて参加する人でも話しやすい質問を考えてください。",
            "初めて参加する人でも話しやすい質問を考えてください。",
            "cancel",
            {},
        ),
        ("Do not cancel the meeting.", "cancel the meeting", "cancel", {}),
        (
            "Do not change the meeting to Sunday.",
            "change the meeting to Sunday",
            "set",
            {"date": "Sunday"},
        ),
        (
            "Change the screening time to 11 AM.",
            "Change the screening time to 11 AM.",
            "set",
            {"time": "11 AM"},
        ),
        ('A friend wrote: "Hello. Cancel the meeting."', "Cancel the meeting.", "cancel", {}),
        ("If it rains, we meet at the café.", "we meet at the café.", "set", {"place": "café"}),
    ],
)
def test_a_verbatim_quote_cannot_authorize_a_non_decision(
    tmp_path, monkeypatch, text, quote, kind, values
):
    scripted_llm_availability(monkeypatch)
    with Client.initialize(tmp_path) as client:
        scene = client.conversations.create(ConversationInput(title="Reading group", locale="en"))
        first = "We will meet on Friday at Town Hall."
        model_responses(
            client,
            monkeypatch,
            [
                reply({"kind": "set", "quote": first, "date": "Friday", "place": "Town Hall"}),
                reply({"kind": kind, "quote": quote, **values}, text="I have changed the meeting."),
                reply(text="We can discuss that separately."),
            ],
        )
        original = client.conversations.send(
            scene.id,
            SendInput(operation_id=uuid4(), expected_revision=0, text=first, allow_real_llm=True),
        )
        assert original.state == "complete"
        latest = client.conversations.send(
            scene.id,
            SendInput(operation_id=uuid4(), expected_revision=1, text=text, allow_real_llm=True),
        )
        assert latest.state == "complete" and len(client.llm_records(latest.id)) == 2
        before, after = client.conversations.get(scene.id).turns
        assert after.meeting is None and after.meeting_state == before.meeting_state


@pytest.mark.parametrize("join", ["? ", " and "])
def test_unrecognized_move_still_answers_separate_questions_after_arrival(
    tmp_path, monkeypatch, join
):
    scripted_llm_availability(monkeypatch)
    with Client.initialize(tmp_path) as client:
        scene = client.conversations.create(
            ConversationInput(title="Reading group", locale="en", initial_state={"place": "Shop"})
        )
        travel = "Let's go to Central Park together now."
        inquiry = "What is the meeting plan"
        other = "how should we greet the guests?"
        message = f"{inquiry}{join}{other}"
        answer = "Welcome them by name and offer a seat."
        model_responses(
            client,
            monkeypatch,
            [
                reply(query=other, text="We can discuss that."),
                {"response": reply(query=message)["response"]},
                {"response": reply(query=inquiry, text=answer)["response"]},
            ],
        )
        operation = client.conversations.send(
            scene.id,
            SendInput(
                operation_id=uuid4(),
                expected_revision=0,
                text=f"{travel} {message}",
                allow_real_llm=True,
            ),
        )
        assert operation.state == "complete"
        saved = client.conversations.get(scene.id)
        assert saved.state.place == "Central Park" and len(saved.actions) == 1
        assert saved.turns[0].assistant == answer
        assert saved.turns[0].meeting.status == "unknown"
        calls = client.llm_records(operation.id)
        assert len(calls) == 3
        content = json.loads(calls[1]["messages"][-1]["content"])
        assert content["message"] == message
