"""Exercise metadata, unknown usage and bounded correction with a standard fake chat model."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client
from komaquill.domain.errors import KomaquillError
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel


class Output(BaseModel):
    text: str


def test_language_contract_handles_text_only_schemas_and_records_language_failure(
    tmp_path, monkeypatch
):
    from komaquill.adapters.llm.execution import response_schema
    from komaquill.adapters.llm.language import check_language

    schema = response_schema(Output, "Japanese")
    assert set(schema["properties"]) == {"text"}
    assert "Japanese" in schema["properties"]["text"]["description"]
    execution = Client.initialize(tmp_path).library.creator.execution
    model = execution.settings.llm_profiles["lmstudio-4b"].model
    fake = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"text":"This response uses the wrong language."}',
                response_metadata={"model_name": model},
            )
        ]
    )
    monkeypatch.setattr(execution, "_model", lambda _: fake)
    operation = uuid4()
    with pytest.raises(KomaquillError, match="invalid_response"):
        execution.invoke(
            Output,
            operation,
            "lmstudio-4b",
            True,
            "Policy",
            [("user", "Input")],
            validate=lambda value: check_language(value.text, "ja"),
            repair_attempts=0,
        )
    path = tmp_path / ".artifacts/data/conversation-calls" / str(operation) / "0.json"
    record = json.loads(path.read_bytes())
    assert record["failure_kind"] == "language" and record["validation"] == "rejected"
    assert record["response"] == '{"text":"This response uses the wrong language."}'


def test_usage_aggregates_unique_calls_and_keeps_missing_values_unknown(tmp_path, monkeypatch):
    client = Client.initialize(tmp_path)
    execution = client.library.creator.execution
    model = execution.settings.llm_profiles["lmstudio-4b"].model
    fake = FakeMessagesListChatModel(
        responses=[
            AIMessage(content="invalid", response_metadata={"model_name": model}),
            AIMessage(
                content='{"text":"Accepted"}',
                response_metadata={"model_name": model},
                usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            ),
        ]
    )
    monkeypatch.setattr(execution, "_model", lambda _: fake)
    operation = uuid4()
    value = execution.invoke(Output, operation, "lmstudio-4b", True, "Policy", [("user", "Input")])
    assert value.text == "Accepted"
    path = tmp_path / ".artifacts/data/conversation-usage" / f"{operation}.json"
    usage = json.loads(path.read_bytes())
    assert usage["call_count"] == 2 and usage["unknown_calls"] == 1
    assert usage["known_usage"]["total_tokens"] == 25 and not usage["usage_complete"]
    execution._record_usage(operation)
    assert json.loads(path.read_bytes()) == usage


@pytest.mark.parametrize("reason", ["length", "content_filter", "refusal"])
def test_terminal_response_keeps_raw_and_classification_without_repair(
    tmp_path, monkeypatch, reason
):
    execution = Client.initialize(tmp_path).library.creator.execution
    model = execution.settings.llm_profiles["lmstudio-4b"].model
    raw = '{"text":"Incomplete"}'
    fake = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content=raw,
                response_metadata={"model_name": model, "finish_reason": reason},
                additional_kwargs={"refusal": "Declined"} if reason == "refusal" else {},
            )
        ]
    )
    monkeypatch.setattr(execution, "_model", lambda _: fake)
    operation = uuid4()
    with pytest.raises(KomaquillError, match="invalid_response"):
        execution.invoke(Output, operation, "lmstudio-4b", True, "Policy", [("user", "Input")])
    files = list((tmp_path / ".artifacts/data/conversation-calls" / str(operation)).glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_bytes())
    assert record["response"] == raw
    assert record["failure_kind"] == ("truncated" if reason == "length" else reason)


def test_strict_validation_rejects_coercion_and_records_the_repair(tmp_path, monkeypatch):

    class Quantity(BaseModel):
        count: int

    execution = Client.initialize(tmp_path).library.creator.execution
    model = execution.settings.llm_profiles["lmstudio-4b"].model
    fake = FakeMessagesListChatModel(
        responses=[
            AIMessage(content=raw, response_metadata={"model_name": model})
            for raw in ('{"count":"2"}', '{"count":2}')
        ]
    )
    monkeypatch.setattr(execution, "_model", lambda _: fake)
    operation = uuid4()
    assert (
        execution.invoke(
            Quantity,
            operation,
            "lmstudio-4b",
            True,
            "Policy",
            [("user", "Input")],
        ).count
        == 2
    )
    directory = tmp_path / ".artifacts/data/conversation-calls" / str(operation)
    rejected = json.loads((directory / "0.json").read_bytes())
    assert rejected["validation"] == "rejected" and rejected["failure_kind"] == "schema"
    assert json.loads((directory / "1.json").read_bytes())["validation"] == "accepted"


def test_multiple_translations_keep_all_calls_and_usage_for_the_same_operation(
    tmp_path, monkeypatch
):
    execution = Client.initialize(tmp_path).library.creator.execution
    model = execution.settings.llm_profiles["lmstudio-4b"].model
    fake = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content='{"text":"Translated"}',
                response_metadata={"model_name": model},
                usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
        ]
    )
    monkeypatch.setattr(execution, "_model", lambda _: fake)
    operation = uuid4()
    for text in ("Scene", "Subject", "Negative"):
        execution.invoke(
            Output, operation, "lmstudio-4b", True, "Policy", [("user", text)], purpose="image-"
        )
    directory = tmp_path / ".artifacts/data/conversation-calls" / str(operation)
    records = [json.loads(path.read_bytes()) for path in directory.glob("*.json")]
    assert len({record["invocation_id"] for record in records}) == 3
    assert {record["messages"][-1]["content"] for record in records} == {
        "Scene",
        "Subject",
        "Negative",
    }
    usage = json.loads(
        (tmp_path / ".artifacts/data/conversation-usage" / f"{operation}.json").read_bytes()
    )
    assert usage["call_count"] == 3 and usage["known_usage"]["total_tokens"] == 36
    assert usage["usage_complete"]
