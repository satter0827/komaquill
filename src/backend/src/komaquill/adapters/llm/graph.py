"""Run bounded structured requests with operation-scoped checkpoints."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langsmith import tracing_context
from pydantic import BaseModel

from komaquill.adapters.llm.context import ContextBudget
from komaquill.domain.errors import KomaquillError

WORKFLOW_VERSION = 2


class RequestState(TypedDict):
    messages: list[BaseMessage]
    attempt: int
    response: str
    value: dict[str, Any] | None
    correction: str
    version: int


@dataclass
class RequestContext:
    send: Callable[[list[BaseMessage], int], str]
    parse: Callable[[str], BaseModel]
    correction: Callable[[ValueError], str]
    budget: ContextBudget
    repair_attempts: int


def request(state: RequestState, runtime: Runtime[RequestContext]) -> dict[str, object]:
    if state["version"] != WORKFLOW_VERSION:
        raise KomaquillError(code="operation_conflict", retryable=False)
    runtime.context.budget.check(state["messages"])
    return {"response": runtime.context.send(state["messages"], state["attempt"])}


def validate(state: RequestState, runtime: Runtime[RequestContext]) -> dict[str, object]:
    try:
        value = runtime.context.parse(state["response"])
        return {"value": value.model_dump(mode="json", exclude_unset=True), "correction": ""}
    except ValueError as error:
        return {"value": None, "correction": runtime.context.correction(error)}


def route(state: RequestState, runtime: Runtime[RequestContext]) -> Literal["complete", "repair"]:
    if state["value"] is not None:
        return "complete"
    if state["attempt"] >= runtime.context.repair_attempts:
        raise KomaquillError(code="invalid_response", retryable=False)
    return "repair"


def repair(state: RequestState) -> dict[str, object]:
    return {
        "attempt": state["attempt"] + 1,
        "messages": [
            *state["messages"],
            AIMessage(content=state["response"]),
            HumanMessage(content=state["correction"]),
        ],
    }


def compile_request(checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    builder = StateGraph(RequestState, context_schema=RequestContext)
    builder.add_node("request", request)
    builder.add_node("validate", validate)
    builder.add_node("repair", repair)
    builder.add_edge(START, "request")
    builder.add_edge("request", "validate")
    builder.add_conditional_edges("validate", route, {"complete": END, "repair": "repair"})
    builder.add_edge("repair", "request")
    return builder.compile(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver()
    )


def run_request[T: BaseModel](
    messages: list[BaseMessage],
    send: Callable[[list[BaseMessage], int], str],
    parse: Callable[[str], T],
    correction: Callable[[ValueError], str],
    *,
    repair_attempts: int,
    budget: ContextBudget,
    trim: bool = True,
    graph: Any = None,
    thread_id: str | None = None,
    schema: type[T] | None = None,
) -> T:
    context = budget.select(messages, trim=trim)
    graph = graph or compile_request()
    config = {
        "configurable": {"thread_id": thread_id or str(uuid4()), "checkpoint_ns": ""},
        "callbacks": [],
        "recursion_limit": 3 * (repair_attempts + 1) + 1,
    }
    saved = graph.get_state(config) if graph.checkpointer else None
    if saved and saved.values and saved.values.get("version") != WORKFLOW_VERSION:
        raise KomaquillError(code="operation_conflict", retryable=False)
    initial = RequestState(
        messages=context,
        attempt=0,
        response="",
        value=None,
        correction="",
        version=WORKFLOW_VERSION,
    )
    with tracing_context(enabled=False):
        result = graph.invoke(
            None if saved and saved.values else initial,
            config=config,
            context=RequestContext(send, parse, correction, budget, repair_attempts),
            durability="sync",
        )
    if schema is not None:
        return schema.model_validate_json(json.dumps(result["value"]), strict=True)
    return parse(json.dumps(result["value"]))
