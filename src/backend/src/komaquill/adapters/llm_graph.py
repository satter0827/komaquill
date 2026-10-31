"""Run one structured request and its bounded corrections with LangGraph."""

from collections.abc import Callable
from typing import Literal, TypedDict, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, trim_messages
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import BaseModel

from komaquill.domain.errors import KomaquillError

WORKFLOW_VERSION = 1


class RequestState(TypedDict):
    messages: list[BaseMessage]
    attempt: int
    response: str
    value: BaseModel | None
    correction: str


def run_request[T: BaseModel](
    messages: list[BaseMessage],
    send: Callable[[list[BaseMessage], int], str],
    parse: Callable[[str], T],
    correction: Callable[[ValueError], str],
    *,
    repair_attempts: int,
    max_input_characters: int,
    schema_characters: int,
) -> T:
    """Keep transport errors terminal and retain complete messages when trimming."""
    budget = max_input_characters - schema_characters

    def characters(items: list[BaseMessage]) -> int:
        return sum(len(str(item.content)) for item in items)

    if characters([messages[0], messages[-1]]) > budget:
        raise KomaquillError(code="context_limit_exceeded", retryable=False)
    context = trim_messages(
        messages,
        max_tokens=budget,
        token_counter=characters,
        strategy="last",
        start_on="human",
        include_system=True,
        allow_partial=False,
    )

    def request(state: RequestState) -> dict[str, object]:
        if characters(state["messages"]) > budget:
            raise KomaquillError(code="context_limit_exceeded", retryable=False)
        return {"response": send(state["messages"], state["attempt"])}

    def validate(state: RequestState) -> dict[str, object]:
        try:
            return {"value": parse(state["response"]), "correction": ""}
        except ValueError as error:
            return {"value": None, "correction": correction(error)}

    def route(state: RequestState) -> Literal["complete", "repair"]:
        if state["value"] is not None:
            return "complete"
        if state["attempt"] >= repair_attempts:
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

    builder = StateGraph(RequestState)
    builder.add_node("request", request)
    builder.add_node("validate", validate)
    builder.add_node("repair", repair)
    builder.add_edge(START, "request")
    builder.add_edge("request", "validate")
    builder.add_conditional_edges("validate", route, {"complete": END, "repair": "repair"})
    builder.add_edge("repair", "request")
    graph = builder.compile()
    with tracing_context(enabled=False):
        result = graph.invoke(
            RequestState(messages=context, attempt=0, response="", value=None, correction=""),
            config={"callbacks": [], "recursion_limit": 3 * (repair_attempts + 1) + 1},
        )
    return cast(T, result["value"])
