"""Verify llm contracts within the declared test scope."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from tests.backend.support.llm_tokenization_fixtures import native, profile
from tests.backend.support.storage import call_file


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
    record = json.loads(call_file(tmp_path, operation).read_bytes())
    assert record["input_tokens_counted"] == (30 if schema_in_context else 15)
    assert record["input_tokens_estimated"] is None and record["usage"] is None
    api.close()


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
