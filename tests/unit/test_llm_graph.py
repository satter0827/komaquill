"""Verify bounded graph routing without network requests or persistent graph state."""

import pytest
from komaquill.adapters.llm_graph import run_request
from komaquill.domain.errors import KomaquillError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langsmith.run_helpers import get_tracing_context, tracing_context
from pydantic import BaseModel


class Result(BaseModel):
    text: str


@pytest.mark.parametrize("repairs,valid_after", [(0, 0), (0, 1), (1, 1), (1, 2)])
def test_graph_bounds_calls_and_preserves_the_rejected_response(repairs, valid_after):
    calls = []

    def send(messages, attempt):
        calls.append((list(messages), attempt))
        return '{"text":"Done"}' if attempt >= valid_after else "invalid"

    def run():
        return run_request(
            [SystemMessage(content="Policy"), HumanMessage(content="Current request")],
            send,
            Result.model_validate_json,
            lambda _: "Correct the response.",
            repair_attempts=repairs,
            max_input_characters=1000,
            schema_characters=100,
        )

    if valid_after <= repairs:
        assert run().text == "Done"
    else:
        with pytest.raises(KomaquillError, match="invalid_response"):
            run()
    assert [attempt for _, attempt in calls] == list(range(min(valid_after, repairs) + 1))
    if len(calls) == 2:
        assert calls[1][0][-2].content == "invalid"
        assert calls[1][0][-1].content == "Correct the response."
        assert sum(message.type == "system" for message in calls[1][0]) == 1


@pytest.mark.parametrize("oversized", ["system", "latest", "repair", None])
def test_graph_retains_fixed_input_and_complete_recent_pairs_within_budget(oversized):
    messages = [
        SystemMessage(content="p" * (200 if oversized == "system" else 10)),
        HumanMessage(content="old " + "a" * 90),
        AIMessage(content="old " + "b" * 90),
        HumanMessage(content="recent request"),
        AIMessage(content="recent response"),
        HumanMessage(content="n" * (200 if oversized == "latest" else 10)),
    ]
    before = list(messages)
    calls = []

    def send(context, attempt):
        calls.append(context)
        return "invalid" if oversized == "repair" else '{"text":"Done"}'

    def run():
        return run_request(
            messages,
            send,
            Result.model_validate_json,
            lambda _: "c" * 200,
            repair_attempts=1,
            max_input_characters=200,
            schema_characters=50,
        )

    if oversized:
        with pytest.raises(KomaquillError, match="context_limit_exceeded"):
            run()
        assert len(calls) == (1 if oversized == "repair" else 0)
    else:
        assert run().text == "Done"
        assert [message.type for message in calls[0]] == ["system", "human", "ai", "human"]
        assert calls[0][0] == messages[0] and calls[0][-1] == messages[-1]
        assert calls[0][1:3] == messages[3:5]
    assert messages == before


def test_graph_does_not_retry_transport_failures_or_trace_under_an_enabled_parent():
    calls = []

    def send(*_):
        calls.append(get_tracing_context()["enabled"])
        raise KomaquillError(code="llm_unavailable", retryable=False)

    with (
        tracing_context(enabled=True),
        pytest.raises(KomaquillError, match="llm_unavailable"),
    ):
        run_request(
            [SystemMessage(content="Policy"), HumanMessage(content="Request")],
            send,
            Result.model_validate_json,
            lambda _: "Correct the response.",
            repair_attempts=1,
            max_input_characters=1000,
            schema_characters=100,
        )
    assert calls == [False]
