"""Count the rendered prompt with an explicitly matched, already loaded model."""

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from langchain_core.messages import BaseMessage

from komaquill.adapters.llm_configuration import LLMProfile
from komaquill.domain.errors import KomaquillError


class NativeTokens:
    def __init__(self, profile: LLMProfile) -> None:
        try:
            import lmstudio
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None
        self.client = lmstudio.Client(api_host=urlsplit(profile.base_url or "").netloc)
        try:
            self.model = next(
                (
                    model
                    for model in self.client.llm.list_loaded()
                    if model.identifier == profile.model
                ),
                None,
            )
            if self.model is None:
                raise KomaquillError(code="llm_unavailable", retryable=False)
            self.window_tokens = self.model.get_context_length()
        except Exception:
            self.client.close()
            raise KomaquillError(code="llm_unavailable", retryable=False) from None

    def __call__(self, messages: Sequence[BaseMessage]) -> int:
        if self.model is None:
            raise KomaquillError(code="llm_unavailable", retryable=False)
        roles = {"human": "user", "ai": "assistant", "system": "system"}
        if any(
            message.type not in roles or not isinstance(message.content, str)
            for message in messages
        ):
            raise KomaquillError(code="invalid_request", retryable=False)
        history: Any = {
            "messages": [{"role": roles[m.type], "content": m.content} for m in messages]
        }
        try:
            rendered = self.model.apply_prompt_template(history)
            return self.model.count_tokens(rendered)
        except Exception:
            raise KomaquillError(code="llm_unavailable", retryable=False) from None

    def close(self) -> None:
        self.client.close()
