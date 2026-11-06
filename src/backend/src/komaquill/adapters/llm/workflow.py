"""Retain the exact conversation input across summary and reply execution."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langsmith import tracing_context

from komaquill.adapters.persistence.checkpoints import CONVERSATION_WORKFLOW_VERSION, Checkpoints
from komaquill.domain.conversations.models import Conversation, Reply, TurnRequest
from komaquill.domain.errors import KomaquillError


class ConversationState(TypedDict):
    conversation: dict[str, Any]
    request: dict[str, Any]
    result: dict[str, Any] | None
    version: int


@dataclass
class ConversationContext:
    reply: Callable[[Conversation, TurnRequest], Reply]


def reply(state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, object]:
    if state["version"] != CONVERSATION_WORKFLOW_VERSION:
        raise KomaquillError(code="operation_conflict", retryable=False)
    conversation = Conversation.model_validate_json(json.dumps(state["conversation"]))
    request = TurnRequest.model_validate_json(json.dumps(state["request"]))
    result = runtime.context.reply(conversation, request)
    return {"result": result.model_dump(mode="json", exclude_unset=True)}


def compile_conversation(checkpoints: Checkpoints) -> Any:
    builder = StateGraph(ConversationState, context_schema=ConversationContext)
    builder.add_node("reply", reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=checkpoints.saver)


def run_conversation(
    graph: Any,
    conversation: Conversation,
    request: TurnRequest,
    respond: Callable[[Conversation, TurnRequest], Reply],
) -> Reply:
    config = {
        "configurable": {"thread_id": f"conversation:{request.operation_id}", "checkpoint_ns": ""},
        "callbacks": [],
    }
    snapshot = graph.get_state(config)
    if snapshot.values and (
        snapshot.values.get("version") != CONVERSATION_WORKFLOW_VERSION
        or snapshot.values["request"] != request.model_dump(mode="json")
        or snapshot.values["conversation"]["id"] != str(conversation.id)
        or snapshot.values["conversation"]["revision"] != conversation.revision
    ):
        raise KomaquillError(code="operation_conflict", retryable=False)
    initial = ConversationState(
        conversation=conversation.model_dump(mode="json"),
        request=request.model_dump(mode="json"),
        result=None,
        version=CONVERSATION_WORKFLOW_VERSION,
    )
    with tracing_context(enabled=False):
        state = graph.invoke(
            None if snapshot.values else initial,
            config=config,
            durability="sync",
            context=ConversationContext(respond),
        )
    return Reply.model_validate_json(json.dumps(state["result"]))
