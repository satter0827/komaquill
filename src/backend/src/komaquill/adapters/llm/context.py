"""Apply one conservative token estimate and independent character safety limits."""

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately

from komaquill.domain.errors import KomaquillError

ESTIMATED_CHARACTERS_PER_TOKEN = 1.0


def tokens(messages: Sequence[BaseMessage]) -> int:
    """Estimate conservatively without downloading a model-specific tokenizer."""
    return count_tokens_approximately(messages, chars_per_token=ESTIMATED_CHARACTERS_PER_TOKEN)


@dataclass(frozen=True)
class ContextBudget:
    max_characters: int
    window_tokens: int
    output_tokens: int
    repair_tokens: int
    schema: str

    @property
    def schema_tokens(self) -> int:
        return tokens([HumanMessage(content=self.schema)])

    @property
    def input_tokens(self) -> int:
        return self.window_tokens - self.output_tokens - self.repair_tokens - self.schema_tokens

    def check(self, messages: Sequence[BaseMessage]) -> None:
        if (
            sum(len(str(item.content)) for item in messages) + len(self.schema)
            > self.max_characters
            or tokens(messages) > self.input_tokens
        ):
            raise KomaquillError(code="context_limit_exceeded", retryable=False)

    def select(self, messages: list[BaseMessage], *, trim: bool) -> list[BaseMessage]:
        self.check([messages[0], messages[-1]])
        if not trim:
            self.check(messages)
            return list(messages)
        context = trim_messages(
            messages,
            max_tokens=self.max_characters - len(self.schema),
            token_counter=lambda items: sum(len(str(item.content)) for item in items),
            strategy="last",
            start_on="human",
            include_system=True,
            allow_partial=False,
        )
        context = trim_messages(
            context,
            max_tokens=self.input_tokens,
            token_counter=tokens,
            strategy="last",
            start_on="human",
            include_system=True,
            allow_partial=False,
        )
        self.check(context)
        return context
