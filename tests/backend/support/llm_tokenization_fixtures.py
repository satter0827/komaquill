"""Share deterministic fixtures used by multiple test scopes."""

from types import SimpleNamespace

import pytest
from komaquill.adapters.llm_configuration import LLMProfile


def profile(**changes):
    return LLMProfile.model_validate(
        {
            "provider": "openai-compatible",
            "model": "fixture",
            "base_url": "http://127.0.0.1:1234/v1",
            "require_model_match": True,
            "token_counting": "lmstudio",
            **changes,
        }
    )


def native(monkeypatch, *, loaded=True, broken=False):
    import lmstudio

    calls = []
    model = SimpleNamespace(
        identifier="fixture",
        get_context_length=lambda: 8192,
        apply_prompt_template=lambda history: calls.append(history) or "rendered prompt",
        count_tokens=lambda text: len(text),
    )
    if broken:
        model.count_tokens = lambda _: (_ for _ in ()).throw(RuntimeError("private SDK error"))
    client = SimpleNamespace(
        llm=SimpleNamespace(
            list_loaded=lambda: [model] if loaded else [],
            model=lambda *_: pytest.fail("Counting must not load models"),
        ),
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(lmstudio, "Client", lambda **_: client)
    return calls
