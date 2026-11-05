"""Count the rendered prompt with an explicitly matched, already loaded model."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
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
        self._client = lmstudio.Client
        self._api_host = urlsplit(profile.base_url or "").netloc
        self._identifier = profile.model
        try:
            with self._loaded_model() as model:
                self.window_tokens = model.get_context_length()
        except Exception:
            raise KomaquillError(
                code="llm_unavailable", stage="model_discovery", retryable=False
            ) from None

    @contextmanager
    def _loaded_model(self) -> Iterator[Any]:
        client = None
        try:
            client = self._client(api_host=self._api_host)
            model = next(
                (
                    model
                    for model in client.llm.list_loaded()
                    if model.identifier == self._identifier
                ),
                None,
            )
            if model is None:
                raise KomaquillError(
                    code="llm_unavailable", stage="model_discovery", retryable=False
                )
        except Exception:
            if client is not None:
                with suppress(Exception):
                    client.close()
            raise KomaquillError(
                code="llm_unavailable", stage="model_discovery", retryable=False
            ) from None
        try:
            yield model
        finally:
            with suppress(Exception):
                client.close()

    def __call__(self, messages: Sequence[BaseMessage]) -> int:
        if not messages:
            return 0
        roles = {"human": "user", "ai": "assistant", "system": "system"}
        if any(
            message.type not in roles or not isinstance(message.content, str)
            for message in messages
        ):
            raise KomaquillError(code="invalid_request", retryable=False)
        history: Any = {
            "messages": [{"role": roles[m.type], "content": m.content} for m in messages]
        }
        if all(message.type == "system" for message in messages):
            # Trimming counts the system prefix alone; templates require a user turn.
            # The empty turn only adds conservative overhead to that partial count.
            history["messages"].append({"role": "user", "content": ""})
        with self._loaded_model() as model:
            try:
                rendered = model.apply_prompt_template(history)
            except Exception:
                raise KomaquillError(
                    code="llm_unavailable", stage="prompt_template", retryable=False
                ) from None
            try:
                return model.count_tokens(rendered)
            except Exception:
                raise KomaquillError(
                    code="llm_unavailable", stage="tokenization", retryable=False
                ) from None
