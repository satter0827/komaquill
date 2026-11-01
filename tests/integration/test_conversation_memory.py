"""Keep historical summaries durable, bounded and separate from current state."""

import json
from uuid import uuid4

import pytest
from komaquill.adapters.database import conversations
from komaquill.api import Client, ConversationInput, SendInput
from komaquill.conversations.models import Turn
from komaquill.domain.roleplay import ConversationMemory
from langchain_core.messages import AIMessage


def prepare(tmp_path, monkeypatch, *, locale="en", failure=None, count=4):
    import langchain_openai

    api = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text("[conversation]\ncontext_turns=4\n", encoding="utf-8")
    calls = []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            self.summary = (
                "narration" not in options["response_format"]["json_schema"]["schema"]["properties"]
            )
            self.limit = options["max_completion_tokens"]
            return self

        def invoke(self, messages, config):
            calls.append((self.summary, list(messages), self.limit))
            if self.summary and failure == "transport":
                raise RuntimeError("Injected transport failure")
            if self.summary:
                result = {
                    "text": "They promised to visit the lighthouse. The player owns the red coat."
                    if locale == "en"
                    else "二人は灯台を訪れる約束をした。赤いコートの持ち主は旅人である。"
                }
                if failure == "invalid":
                    result = {"wrong": "value"}
            else:
                result = {
                    "current_state": api.conversations.get(saved.id).state.model_dump(mode="json"),
                    "text": "Let us go to the lighthouse."
                    if locale == "en"
                    else "約束の灯台へ行きましょう。",
                    "narration": "The waves wash over the beach."
                    if locale == "en"
                    else "海辺で波の音が響いている。",
                }
            return AIMessage(
                content=json.dumps(result, ensure_ascii=False),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    saved = api.conversations.create(
        ConversationInput(
            title="Memory",
            locale=locale,
            initial_state={
                "place": "sea",
                "player": {"clothing": "red coat"},
                "character": {"clothing": "black apron"},
            },
        )
    )
    turns = [
        Turn(
            operation_id=uuid4(),
            user=f"Visit the lighthouse. {index}",
            assistant="We promised to go.",
            narration="The beach is quiet.",
            state=saved.state,
        )
        for index in range(count)
    ]
    value = saved.model_copy(update={"turns": turns, "revision": count})
    with api.conversations.repository.database.engine.begin() as connection:
        connection.execute(conversations.update().values(payload=value.model_dump_json()))
    return api, value, calls


@pytest.mark.parametrize("locale", ["ja", "en"])
def test_summary_is_saved_and_reused_after_restart_without_rewriting_history(
    tmp_path, monkeypatch, locale
):
    api, original, calls = prepare(tmp_path, monkeypatch, locale=locale)
    request = SendInput(
        operation_id=uuid4(), text="What did we promise?", locale=locale, allow_real_llm=True
    )
    assert api.conversations.send(original.id, request).state == "complete"
    saved = Client(tmp_path).conversations.get(original.id)
    assert saved.turns[:4] == original.turns and saved.memory.through_turn == 2
    assert saved.state == original.state
    assert [summary for summary, _, _ in calls] == [True, False]
    assert calls[0][2] == 512
    assert saved.memory.source_version == 2
    summary_messages = calls[0][1]
    confirmed = [
        json.loads(message.content)["confirmed_state"]
        for message in summary_messages
        if message.type == "ai"
    ]
    assert confirmed == [turn.state.model_dump(mode="json") for turn in original.turns[:2]]
    assert "confirmed_state" in summary_messages[0].content
    assert "human_player" in summary_messages[0].content
    assert sum(item.type == "system" for item in calls[1][1]) == 1
    assert "lighthouse" in str(calls[1][1]) or "灯台" in str(calls[1][1])
    assert Client(tmp_path).conversations.send(original.id, request).state == "complete"
    assert len(calls) == 2
    next_request = SendInput(
        operation_id=uuid4(), text="Continue", locale=locale, allow_real_llm=True
    )
    assert Client(tmp_path).conversations.send(original.id, next_request).state == "complete"
    assert [summary for summary, _, _ in calls] == [True, False, False]
    assert Client(tmp_path).conversations.get(original.id).memory == saved.memory


@pytest.mark.parametrize(
    "failure,code", [("transport", "llm_unavailable"), ("invalid", "invalid_response")]
)
def test_summary_failure_does_not_retry_or_send_dialogue_or_lose_history(
    tmp_path, monkeypatch, failure, code
):
    api, original, calls = prepare(tmp_path, monkeypatch, failure=failure)
    operation = api.conversations.send(
        original.id, SendInput(operation_id=uuid4(), text="Continue", allow_real_llm=True)
    )
    assert operation.error == code and len(calls) == 1
    assert api.conversations.get(original.id) == original


def test_short_history_does_not_call_summary_and_permission_is_required(tmp_path, monkeypatch):
    api, original, calls = prepare(tmp_path, monkeypatch, count=1)
    operation = api.conversations.send(
        original.id, SendInput(operation_id=uuid4(), text="Continue", allow_real_llm=True)
    )
    assert (
        operation.state == "complete"
        and len(calls) == 1
        and not any(summary for summary, _, _ in calls)
    )
    operation = api.conversations.send(
        original.id, SendInput(operation_id=uuid4(), text="Continue")
    )
    assert operation.error == "real_llm_not_allowed" and len(calls) == 1


def test_later_summary_advances_from_saved_message_ids_without_resending_old_turns(
    tmp_path, monkeypatch
):
    api, original, calls = prepare(tmp_path, monkeypatch)
    for _ in range(3):
        result = api.conversations.send(
            original.id, SendInput(operation_id=uuid4(), text="Continue", allow_real_llm=True)
        )
        assert result.state == "complete"
    summaries = [messages for summary, messages, _ in calls if summary]
    assert len(summaries) == 2
    current = api.conversations.get(original.id)
    assert current.memory.through_turn == 4
    assert current.turns[:4] == original.turns
    human_text = [item.content for item in summaries[1] if item.type == "human"]
    assert original.turns[0].user not in human_text
    assert original.turns[2].user in human_text


def test_memory_hash_and_scope_are_validated_before_another_call(tmp_path, monkeypatch):
    api, original, calls = prepare(tmp_path, monkeypatch)
    bad = ConversationMemory(
        text="Invalid memory",
        through_turn=2,
        source_sha256="wrong",
        locale="en",
        model="model",
        instructions_sha256="hash",
    )
    api.conversations.repository.save_memory(original.id, bad)
    operation = api.conversations.send(
        original.id, SendInput(operation_id=uuid4(), text="Continue", allow_real_llm=True)
    )
    assert operation.error == "storage_failed" and calls == []
    with pytest.raises(Exception, match="operation_conflict"):
        api.conversations.repository.save_memory(
            original.id, bad.model_copy(update={"through_turn": 99})
        )
    api.conversations.repository.save_memory(
        original.id, bad.model_copy(update={"through_turn": 1})
    )
    assert api.conversations.get(original.id).memory == bad


def test_summary_hash_covers_consumed_text_and_ignores_unconsumed_record_metadata(
    tmp_path, monkeypatch
):
    from komaquill.adapters.llm.memory import history_hash

    _, original, _ = prepare(tmp_path, monkeypatch)
    changed = original.model_copy(deep=True)
    changed.turns[0] = changed.turns[0].model_copy(update={"image_id": uuid4()})
    assert history_hash(changed, 2) == history_hash(original, 2)
    changed.turns[0] = changed.turns[0].model_copy(update={"user": "A different promise"})
    assert history_hash(changed, 2) != history_hash(original, 2)


def test_missing_summary_dependency_is_typed_and_does_not_change_history(tmp_path, monkeypatch):
    import sys

    api, original, calls = prepare(tmp_path, monkeypatch, count=1)
    monkeypatch.setitem(sys.modules, "langmem.short_term", None)
    operation = api.conversations.send(
        original.id, SendInput(operation_id=uuid4(), text="Continue", allow_real_llm=True)
    )
    assert operation.error == "llm_dependencies_missing"
    assert calls == [] and api.conversations.get(original.id) == original


def test_summary_source_versions_preserve_old_hashes_and_cover_confirmed_outcomes(
    tmp_path, monkeypatch
):
    from komaquill.adapters.llm.memory import history_hash

    _, original, _ = prepare(tmp_path, monkeypatch)
    changed = original.model_copy(deep=True)
    changed.turns[0] = changed.turns[0].model_copy(
        update={"state": changed.state.model_copy(update={"place": "Another place"})}
    )
    assert history_hash(changed, 2, 1) == history_hash(original, 2, 1)
    assert history_hash(changed, 2, 2) != history_hash(original, 2, 2)


def test_oversized_required_input_does_not_send_summary_or_dialogue(tmp_path, monkeypatch):
    api, original, calls = prepare(tmp_path, monkeypatch)
    execution = api.conversations.responder.execution
    execution.settings = execution.settings.model_copy(update={"context_window_tokens": 500})
    result = api.conversations.send(
        original.id,
        SendInput(operation_id=uuid4(), text="Continue", allow_real_llm=True),
    )
    assert result.error == "context_limit_exceeded" and calls == []
    saved = api.conversations.get(original.id)
    assert saved.turns == original.turns and saved.memory is None
