"""Verify llm contracts within the declared test scope."""

from types import SimpleNamespace

import pytest
from komaquill.adapters.llm.context import ContextBudget
from komaquill.adapters.llm.tokenization import NativeTokens
from komaquill.domain.errors import KomaquillError
from langchain_core.messages import HumanMessage, SystemMessage

from tests.backend.support.llm_tokenization_fixtures import native, profile


def test_native_counter_uses_the_loaded_model_template_and_closes(monkeypatch):
    calls = native(monkeypatch)
    counter = NativeTokens(profile())
    assert counter.window_tokens == 8192
    assert counter([SystemMessage(content="Policy"), HumanMessage(content="Hello")]) == 15
    assert [item for item in calls if isinstance(item, dict)][0] == {
        "messages": [{"role": "system", "content": "Policy"}, {"role": "user", "content": "Hello"}]
    }
    assert calls[-1] == "closed"


def test_missing_model_is_not_loaded_and_client_is_closed(monkeypatch):
    calls = native(monkeypatch, loaded=False)
    with pytest.raises(KomaquillError, match="llm_unavailable"):
        NativeTokens(profile())
    assert calls == ["closed"]


def test_native_counter_handles_empty_trim_candidates_without_rendering(monkeypatch):
    calls = native(monkeypatch)
    counter = NativeTokens(profile())
    assert counter([]) == 0 and calls == ["closed"]
    messages = [SystemMessage(content="Policy"), HumanMessage(content="Hello")]
    budget = ContextBudget(1000, 8192, 100, 100, "schema", count=counter)
    assert budget.select(messages, trim=True) == messages
    assert all(history["messages"] for history in calls if isinstance(history, dict))


def test_native_counter_adapts_system_only_candidates_without_mutating_input(monkeypatch):
    calls = native(monkeypatch)
    counter = NativeTokens(profile())
    system = [SystemMessage(content="Policy")]
    assert counter(system) == 15
    assert [item for item in calls if isinstance(item, dict)] == [
        {"messages": [{"role": "system", "content": "Policy"}, {"role": "user", "content": ""}]}
    ]
    assert len(system) == 1 and system[0].content == "Policy"


def test_counting_failures_do_not_expose_sdk_details(monkeypatch):
    native(monkeypatch, broken=True)
    counter = NativeTokens(profile())
    with pytest.raises(KomaquillError, match="^llm_unavailable$"):
        counter([HumanMessage(content="Hello")])
    with pytest.raises(KomaquillError, match="invalid_request"):
        counter([HumanMessage(content=[{"type": "text", "text": "Hello"}])])


def test_grammar_schema_is_not_counted_as_prompt_but_keeps_payload_limits():
    budget = ContextBudget(
        1000, 100, 20, 10, "x" * 200, count=lambda _: 30, schema_in_context=False
    )
    assert budget.input_tokens == 70 and budget.schema_tokens == 0
    budget.check([HumanMessage(content="Hello")])
    with pytest.raises(KomaquillError, match="context_limit_exceeded"):
        budget.check([HumanMessage(content="x" * 900)])


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://example.com/v1"},
        {"key_env": "PRIVATE_KEY"},
        {"require_model_match": False},
    ],
)
def test_native_counting_requires_an_explicit_matched_local_connection(changes):
    with pytest.raises(ValueError):
        profile(**changes)


def test_native_counter_renews_sdk_sessions_after_server_restart(monkeypatch):
    import lmstudio

    generation = [0]
    clients = []

    def create(**_):
        current = generation[0]
        closed = []

        def count(text):
            assert not closed and current == generation[0]
            return len(text)

        model = SimpleNamespace(
            identifier="fixture",
            get_context_length=lambda: 8192,
            apply_prompt_template=lambda _: "prompt",
            count_tokens=count,
        )
        client = SimpleNamespace(
            llm=SimpleNamespace(list_loaded=lambda: [model]), close=lambda: closed.append(True)
        )
        clients.append(closed)
        return client

    monkeypatch.setattr(lmstudio, "Client", create)
    counter = NativeTokens(profile())
    assert counter([HumanMessage(content="First")]) == 6
    generation[0] += 1
    assert counter([HumanMessage(content="After restart")]) == 6
    assert len(clients) == 3 and all(closed for closed in clients)
