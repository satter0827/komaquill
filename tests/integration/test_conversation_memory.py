"""Keep historical summaries durable, bounded and separate from current state."""

import json
from uuid import uuid4

import pytest
from komaquill.api import Client, ConversationInput, KomaquillError, SendInput
from komaquill.conversations.models import Turn
from komaquill.domain.roleplay import ConversationMemory
from langchain_core.messages import AIMessage

from tests.support.conversations import wire_reply, wire_scene

pytestmark = pytest.mark.usefixtures("substituted_services")


def prepare(
    tmp_path,
    monkeypatch,
    *,
    locale="en",
    failure=None,
    count=4,
    recent_turns=2,
    action_narration="",
):
    import langchain_openai

    api = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text(
        f"[conversation]\nrecent_turns={recent_turns}\ncontext_window_tokens=1440\n"
        "max_output_tokens=512\nrepair_reserve_tokens=128\n",
        encoding="utf-8",
    )
    calls = []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            self.summary = (
                "response" not in options["response_format"]["json_schema"]["schema"]["properties"]
            )
            self.limit = options["max_completion_tokens"]
            return self

        def invoke(self, messages, config):
            calls.append((self.summary, list(messages), self.limit))
            if self.summary and failure == "transport":
                raise RuntimeError("Injected transport failure")
            if self.summary:
                sources = json.loads(messages[-1].content)
                result = {
                    "passages": [
                        {"source": item["source"], "quote": item["text"]}
                        for item in sources
                        if item["speaker"] == "user"
                    ][:2]
                }
                if failure == "fabricated":
                    result = {"passages": [{"source": 0, "quote": "An invented fact."}]}
                if failure == "invalid":
                    result = {"wrong": "value"}
            else:
                result = {
                    "current_state": wire_scene(api.conversations.get(saved.id).state),
                    "action_narration": "",
                    "requested_player_action": None,
                    "text": "Let us go to the lighthouse."
                    if locale == "en"
                    else "約束の灯台へ行きましょう。",
                }
            return AIMessage(
                content=json.dumps(wire_reply(result), ensure_ascii=False),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    from dataclasses import replace

    from komaquill.adapters.llm.execution import StructuredExecution

    original_budget = StructuredExecution.budget
    monkeypatch.setattr(
        StructuredExecution,
        "budget",
        lambda self, *args: replace(
            original_budget(self, *args),
            count=lambda messages: (
                200
                + sum(
                    200
                    * sum(
                        item["consecutive_turns"]
                        for item in json.loads(m.content).get("history", [])
                    )
                    for m in messages
                    if m.type == "human"
                    and isinstance(m.content, str)
                    and m.content.startswith('{"history":')
                )
            ),
        ),
    )
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
            action_narration=action_narration,
            state=saved.state,
        )
        for index in range(count)
    ]
    value = saved.model_copy(update={"turns": turns, "revision": count, "turn_count": count})
    with api.conversations.repository.database.engine.begin() as connection:
        api.conversations.repository._store(connection, value)
    return api, value, calls


def test_summary_receives_companion_actions_separately_from_scenery_and_dialogue(
    tmp_path, monkeypatch
):
    action = "The companion traces a lighthouse in the sand."
    api, original, calls = prepare(tmp_path, monkeypatch, action_narration=action)
    result = api.conversations.send(
        original.id,
        SendInput(
            expected_revision=api.conversations.overview(original.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert result.state == "complete"
    summary_input = json.loads(calls[0][1][-1].content)
    assert [item["text"] for item in summary_input if item["speaker"] == "action_narration"] == [
        action,
        action,
    ]
    assert [item["text"] for item in summary_input if item["speaker"] == "narration"] == [
        "The beach is quiet."
    ] * 2
    assert api.conversations.get(original.id).turns[:4] == original.turns


@pytest.mark.parametrize("locale", ["ja", "en"])
def test_summary_is_saved_and_reused_after_restart_without_rewriting_history(
    tmp_path, monkeypatch, locale
):
    api, original, calls = prepare(tmp_path, monkeypatch, locale=locale)
    request = SendInput(
        expected_revision=original.revision,
        operation_id=uuid4(),
        text="What did we promise?",
        locale=locale,
        allow_real_llm=True,
    )
    assert api.conversations.send(original.id, request).state == "complete"
    saved = Client(tmp_path).conversations.get(original.id)
    assert saved.turns[:4] == original.turns and saved.memory.through_turn == 2
    assert saved.state == original.state
    assert [summary for summary, _, _ in calls] == [True, False]
    assert calls[0][2] == 512
    assert saved.memory.source_version == 5
    summary_messages = calls[0][1]
    summary_sources = json.loads(summary_messages[-1].content)
    assert {item["speaker"] for item in summary_sources} == {"user", "assistant", "narration"}
    assert saved.memory.text == "\n".join(turn.user for turn in original.turns[:2])
    assert {p.turn_id for p in saved.memory.passages} == {
        str(turn.operation_id) for turn in original.turns[:2]
    }
    assert sum(item.type == "system" for item in calls[1][1]) == 1
    assert "lighthouse" in str(calls[1][1]) or "灯台" in str(calls[1][1])
    assert Client(tmp_path).conversations.send(original.id, request).state == "complete"
    assert len(calls) == 2
    next_request = SendInput(
        expected_revision=saved.revision,
        operation_id=uuid4(),
        text="Continue",
        locale=locale,
        allow_real_llm=True,
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
        original.id,
        SendInput(
            expected_revision=api.conversations.overview(original.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert operation.error == code and len(calls) == 1
    assert api.conversations.get(original.id) == original


def test_short_history_does_not_call_summary_and_permission_is_required(tmp_path, monkeypatch):
    api, original, calls = prepare(tmp_path, monkeypatch, count=1)
    operation = api.conversations.send(
        original.id,
        SendInput(
            expected_revision=api.conversations.overview(original.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert (
        operation.state == "complete"
        and len(calls) == 1
        and not any(summary for summary, _, _ in calls)
    )
    with pytest.raises(KomaquillError, match="real_llm_not_allowed"):
        api.conversations.send(
            original.id,
            SendInput(
                expected_revision=api.conversations.overview(original.id).revision,
                operation_id=uuid4(),
                text="Continue",
                allow_real_llm=False,
            ),
        )
    assert len(calls) == 1


def test_later_summary_advances_from_saved_message_ids_without_resending_old_turns(
    tmp_path, monkeypatch
):
    api, original, calls = prepare(tmp_path, monkeypatch)
    for _ in range(3):
        result = api.conversations.send(
            original.id,
            SendInput(
                expected_revision=api.conversations.overview(original.id).revision,
                operation_id=uuid4(),
                text="Continue",
                allow_real_llm=True,
            ),
        )
        assert result.state == "complete"
    summaries = [messages for summary, messages, _ in calls if summary]
    assert len(summaries) == 2
    current = api.conversations.get(original.id)
    assert current.memory.through_turn == 4
    assert current.turns[:4] == original.turns
    source_text = [item["text"] for item in json.loads(summaries[1][-1].content)]
    assert original.turns[2].user in source_text
    assert original.turns[0].user in source_text


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
        original.id,
        SendInput(
            expected_revision=api.conversations.overview(original.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
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


def test_fabricated_memory_evidence_stops_before_dialogue(tmp_path, monkeypatch):
    api, original, calls = prepare(tmp_path, monkeypatch, failure="fabricated")
    operation = api.conversations.send(
        original.id,
        SendInput(
            expected_revision=api.conversations.overview(original.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert operation.error == "invalid_response" and len(calls) == 1
    assert api.conversations.get(original.id) == original


def test_oversized_required_input_does_not_send_summary_or_dialogue(tmp_path, monkeypatch):
    api, original, calls = prepare(tmp_path, monkeypatch)
    execution = api.conversations.responder.execution
    execution.settings = execution.settings.model_copy(update={"context_window_tokens": 500})
    result = api.conversations.send(
        original.id,
        SendInput(
            expected_revision=api.conversations.overview(original.id).revision,
            operation_id=uuid4(),
            text="Continue",
            allow_real_llm=True,
        ),
    )
    assert result.error == "context_limit_exceeded" and calls == []
    saved = api.conversations.get(original.id)
    assert saved.turns == original.turns and saved.memory is None


@pytest.mark.parametrize("count", [0, 1])
def test_recent_history_does_not_trigger_summary_until_the_budget_is_exceeded(
    tmp_path, monkeypatch, count
):
    api, original, calls = prepare(tmp_path, monkeypatch, count=count, recent_turns=1)
    for index in range(4 - count):
        operation = api.conversations.send(
            original.id,
            SendInput(
                expected_revision=api.conversations.overview(original.id).revision,
                operation_id=uuid4(),
                text=f"Continue {index}",
                allow_real_llm=True,
            ),
        )
        assert operation.state == "complete"
    assert [summary for summary, _, _ in calls] == [False] * (4 - count)
    saved = api.conversations.get(original.id)
    assert len(saved.turns) == 4 and saved.memory is None
    operation = api.conversations.send(
        original.id,
        SendInput(
            expected_revision=api.conversations.overview(original.id).revision,
            operation_id=uuid4(),
            text="Continue again",
            allow_real_llm=True,
        ),
    )
    assert operation.state == "complete"
    assert [summary for summary, _, _ in calls][-2:] == [True, False]
    continued = api.conversations.get(original.id)
    assert continued.turns[:4] == saved.turns
    assert continued.memory.through_turn == 3
