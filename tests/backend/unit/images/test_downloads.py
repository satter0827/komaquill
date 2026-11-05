"""Verify images contracts within the declared test scope."""

from contextlib import contextmanager

import httpx
import pytest
from komaquill.domain.errors import KomaquillError

from tests.backend.support.presets import asset


def test_preflight_uses_streaming_get_for_sources_rejecting_head(monkeypatch):
    from komaquill.adapters import model_auth

    observed = []
    monkeypatch.setattr(model_auth, "credentials", lambda: {})

    @contextmanager
    def stream(method, url, **kwargs):
        observed.append((method, kwargs))
        yield httpx.Response(
            206, headers={"Content-Type": "binary/octet-stream"}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(model_auth.httpx, "stream", stream)
    model_auth.preflight([asset()], 1)
    assert observed[0][0] == "GET"
    assert observed[0][1]["headers"]["Range"] == "bytes=0-0"


@pytest.mark.parametrize(
    "status,content_type,expected",
    [
        (401, "application/json", "authentication_failed"),
        (403, "text/plain", "authentication_failed"),
        (200, "text/html", "authentication_failed"),
        (503, "text/plain", "http_failed"),
    ],
)
def test_preflight_classifies_safe_failures(monkeypatch, status, content_type, expected):
    from komaquill.adapters import model_auth

    monkeypatch.setattr(model_auth, "credentials", lambda: {})

    @contextmanager
    def stream(method, url, **kwargs):
        yield httpx.Response(
            status, headers={"Content-Type": content_type}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(model_auth.httpx, "stream", stream)
    with pytest.raises(KomaquillError) as error:
        model_auth.preflight([asset()], 1)
    assert error.value.code == expected
