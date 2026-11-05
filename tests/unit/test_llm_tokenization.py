"""Verify native counting never loads a model or exposes SDK failures."""

from types import SimpleNamespace

import pytest
from komaquill.adapters.llm.context import ContextBudget
from komaquill.adapters.llm.tokenization import NativeTokens
from komaquill.adapters.llm_configuration import LLMProfile
from komaquill.domain.errors import KomaquillError
from langchain_core.messages import HumanMessage, SystemMessage


def profile(**changes):
    return LLMProfile.model_validate(
        {
            "provider": "openai-compatible",
            "model": "fixture",
            "base_url": "http://127.0.0.1:1234/v1",
            "require_model_match": True,
            "token_counting": "lmstudio",
            **changes,
        }
    )


def native(monkeypatch, *, loaded=True, broken=False):
    import lmstudio

    calls = []
    model = SimpleNamespace(
        identifier="fixture",
        get_context_length=lambda: 8192,
        apply_prompt_template=lambda history: calls.append(history) or "rendered prompt",
        count_tokens=lambda text: len(text),
    )
    if broken:
        model.count_tokens = lambda _: (_ for _ in ()).throw(RuntimeError("private SDK error"))
    client = SimpleNamespace(
        llm=SimpleNamespace(
            list_loaded=lambda: [model] if loaded else [],
            model=lambda *_: pytest.fail("Counting must not load models"),
        ),
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(lmstudio, "Client", lambda **_: client)
    return calls


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


@pytest.mark.parametrize("schema_in_context", [False, True])
def test_native_call_record_accounts_for_the_configured_schema_context(
    tmp_path, monkeypatch, schema_in_context
):
    import json
    from uuid import uuid4

    from komaquill.api import Client
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from pydantic import BaseModel

    class Result(BaseModel):
        text: str

    native(monkeypatch)
    api = Client.initialize(tmp_path)
    execution = api.library.creator.execution
    execution.settings.llm_profiles["lmstudio-4b"] = profile(schema_in_context=schema_in_context)
    fake = FakeMessagesListChatModel(
        responses=[
            AIMessage(content='{"text":"Accepted"}', response_metadata={"model_name": "fixture"})
        ]
    )
    monkeypatch.setattr(execution, "_model", lambda _: fake)
    operation = uuid4()
    execution.invoke(Result, operation, "lmstudio-4b", True, "Policy", [("user", "Hello")])
    record = json.loads(
        (tmp_path / ".artifacts/data/conversation-calls" / str(operation) / "0.json").read_bytes()
    )
    assert record["input_tokens_counted"] == (30 if schema_in_context else 15)
    assert record["input_tokens_estimated"] is None and record["usage"] is None
    api.close()


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


def test_native_budget_refreshes_loaded_context_without_invalidating_previous_budget(
    tmp_path, monkeypatch
):
    import lmstudio
    from komaquill.api import Client

    window = [8192]
    models = []

    def create(**_):
        closed = []
        model = SimpleNamespace(
            identifier="fixture",
            get_context_length=lambda: window[0],
            apply_prompt_template=lambda _: "prompt",
            count_tokens=lambda text: len(text),
        )
        models.append(closed)
        return SimpleNamespace(
            llm=SimpleNamespace(list_loaded=lambda: [model]), close=lambda: closed.append(True)
        )

    monkeypatch.setattr(lmstudio, "Client", create)
    with Client.initialize(tmp_path) as api:
        execution = api.library.creator.execution
        execution.settings.llm_profiles["lmstudio-4b"] = profile()
        first = execution.budget("{}", "lmstudio-4b")
        window[0] = 4096
        second = execution.budget("{}", "lmstudio-4b")
        assert first.window_tokens == 8192 and second.window_tokens == 4096
        assert first.count([HumanMessage(content="Retained")]) == 6
    assert all(closed for closed in models)
