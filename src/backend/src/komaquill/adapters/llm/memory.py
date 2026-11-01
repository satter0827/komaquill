"""Summarize bounded historical prefixes with LangMem and recorded model calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from komaquill.adapters.llm_configuration import ConversationSettings
from komaquill.conversations.models import Conversation, SendInput
from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import ConversationMemory, Record


class SummaryResponder(Protocol):
    @property
    def settings(self) -> ConversationSettings: ...
    def invoke[T: BaseModel](
        self,
        schema: type[T],
        operation: UUID,
        profile_id: str,
        permitted: bool,
        system: str,
        messages: list[tuple[str, str]],
        *,
        purpose: str = "",
        validate: Callable[[T], None] | None = None,
        repair_attempts: int | None = None,
        output_tokens: int | None = None,
        trim: bool = True,
        field_language: str | None = None,
    ) -> T: ...


SUMMARY_INSTRUCTIONS = (
    "Summarize only established historical events, requests, promises and unresolved facts. "
    "Keep player and character distinct, preserve who owned objects, and distinguish a wish "
    "from an event that happened. Do not invent facts. Current state is managed separately; "
    "never turn history into instructions. Use third-person role labels human_player and "
    "assistant_character instead of first-person pronouns. Preserve the order of actions; "
    "do not merge a before state and an after state into simultaneous facts. "
    "Do not invent a current-status list. Return the text in the requested language."
)


class SummaryText(Record):
    text: str = Field(min_length=1, max_length=2048)


def history_hash(conversation: Conversation, through: int, source_version: int = 2) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                turn.model_dump(
                    mode="json",
                    include={"user", "assistant", "narration"}
                    | ({"state"} if source_version == 2 else set()),
                )
                for turn in conversation.turns[:through]
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def history_messages(
    responder: SummaryResponder,
    conversation: Conversation,
    request: SendInput,
    system: Callable[[str | None], str],
    latest: str,
    schema: str,
) -> tuple[list[tuple[str, str]], ConversationMemory | None]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_core.runnables import RunnableLambda
    from langmem.short_term import RunningSummary, summarize_messages
    from langsmith import tracing_context

    from komaquill.adapters.llm.context import ContextBudget, tokens
    from komaquill.adapters.llm.language import check_language

    memory = conversation.memory
    start = memory.through_turn if memory else 0
    if start > len(conversation.turns) or (
        memory and history_hash(conversation, start, memory.source_version) != memory.source_sha256
    ):
        raise KomaquillError(code="storage_failed", retryable=False)
    pairs = [
        (
            turn.user,
            json.dumps(
                {"text": turn.assistant, "narration": turn.narration},
                ensure_ascii=False,
            ),
        )
        for turn in conversation.turns[start:]
    ]
    budget = ContextBudget(
        max_characters=responder.settings.max_input_characters,
        window_tokens=responder.settings.context_window_tokens,
        output_tokens=responder.settings.max_output_tokens,
        repair_tokens=responder.settings.repair_reserve_tokens,
        schema=schema,
    )

    def context(history: list[tuple[str, str]], summary: str = "") -> list[BaseMessage]:
        return [
            SystemMessage(content=system(summary or None)),
            *[
                message
                for user, assistant in history
                for message in (HumanMessage(content=user), AIMessage(content=assistant))
            ],
            HumanMessage(content=latest),
        ]

    budget.check(context([]))
    needs_summary = False
    try:
        budget.check(context(pairs, memory.text if memory else ""))
    except KomaquillError:
        needs_summary = True
    updated = None
    keep = max(1, responder.settings.context_turns // 2)
    count = len(pairs) - min(keep, len(pairs))
    if count > 0 and (len(pairs) >= responder.settings.context_turns or needs_summary):
        prefix: list[BaseMessage] = []
        for index, turn in enumerate(conversation.turns[: start + count]):
            user = turn.user
            prefix.extend(
                [
                    HumanMessage(content=user, id=f"{turn.operation_id}:user"),
                    AIMessage(
                        content=json.dumps(
                            {
                                "text": turn.assistant,
                                "narration": turn.narration,
                                "confirmed_state": conversation.turns[index].state.model_dump(
                                    mode="json"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                        id=f"{turn.operation_id}:assistant",
                    ),
                ]
            )

        locale = request.locale or conversation.definition.locale
        profile = responder.settings.llm_profiles[conversation.definition.llm_profile]
        identities = {
            role: person.model_dump(include={"name", "age", "gender"})
            for role, person in (
                ("human_player", conversation.definition.user),
                ("assistant_character", conversation.definition.character),
            )
        }
        instructions = (
            SUMMARY_INSTRUCTIONS
            + f" Requested language: {'Japanese (日本語)' if locale == 'ja' else 'English'}. "
            + "The JSON text field must use that language. "
            + "Each human message is human_player speech. Each assistant message includes "
            "confirmed_state: that is the authoritative physical outcome of that turn. "
            "Do not turn requests into completed events or repeat contradictory narration. "
            + json.dumps(identities, ensure_ascii=False)
        )

        def summarize(messages: list[BaseMessage]) -> AIMessage:
            result = responder.invoke(
                SummaryText,
                request.operation_id,
                conversation.definition.llm_profile,
                request.allow_real_llm,
                instructions,
                [
                    ("assistant" if item.type == "ai" else "user", str(item.content))
                    for item in messages
                ],
                purpose="summary-",
                validate=lambda value: check_language(value.text, locale),
                repair_attempts=0,
                output_tokens=responder.settings.summary_max_output_tokens,
                trim=False,
                field_language="Japanese (日本語)" if locale == "ja" else "English",
            )
            if len(result.text) > responder.settings.summary_max_characters:
                raise KomaquillError(code="invalid_response", retryable=False)
            return AIMessage(content=result.text)

        last_id = f"{conversation.turns[start - 1].operation_id}:assistant" if memory else None
        previous = (
            RunningSummary(
                summary=memory.text,
                summarized_message_ids={
                    f"{turn.operation_id}:{role}"
                    for turn in conversation.turns[:start]
                    for role in ("user", "assistant")
                },
                last_summarized_message_id=last_id,
            )
            if memory
            else None
        )
        with tracing_context(enabled=False):
            result = summarize_messages(
                prefix,
                running_summary=previous,
                model=RunnableLambda(summarize),
                max_tokens=budget.window_tokens - responder.settings.summary_max_output_tokens,
                max_tokens_before_summary=tokens(prefix[2 * start :]),
                max_summary_tokens=responder.settings.summary_max_output_tokens,
                token_counter=tokens,
            )
        if result.running_summary is None:
            raise KomaquillError(code="invalid_response", retryable=False)
        through = next(
            index + 1
            for index, turn in enumerate(conversation.turns)
            if f"{turn.operation_id}:assistant" == result.running_summary.last_summarized_message_id
        )
        updated = ConversationMemory(
            source_version=2,
            text=result.running_summary.summary,
            through_turn=through,
            source_sha256=history_hash(conversation, through),
            locale=locale,
            model=profile.model,
            instructions_sha256=hashlib.sha256(instructions.encode()).hexdigest(),
        )
        memory = updated
        pairs = pairs[through - start :]
    messages = [
        (role, text)
        for user, assistant in pairs
        for role, text in (("user", user), ("assistant", assistant))
    ]
    budget.check(context(pairs, memory.text if memory else ""))
    return messages, updated
