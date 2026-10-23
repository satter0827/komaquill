"""Treat gateway failures as unknown submission outcomes."""

import httpx
import pytest

from komaquill.client import ComfyClient
from komaquill.errors import KomaquillError, SubmissionUnknown


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502),
        httpx.Response(200, text="invalid JSON"),
        httpx.Response(200, json={"accepted": True}),
    ],
)
def test_unconfirmed_submission_is_not_reported_as_rejected(response):
    client = ComfyClient(
        "https://example.com",
        "test-only-value",
        transport=httpx.MockTransport(lambda _request: response),
    )
    with pytest.raises(SubmissionUnknown):
        client.submit({}, {}, "run-id")
    client.close()


def test_non_json_status_is_a_readable_error():
    client = ComfyClient(
        "https://example.com",
        "test-only-value",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="not JSON")),
    )
    with pytest.raises(KomaquillError, match="応答形式"):
        client.json("/system_stats")
    client.close()


def test_non_unicode_submission_response_remains_unknown_and_is_recovered(
    project, client, session, stub, monkeypatch
):
    import httpx

    from komaquill.service import collect, generate

    original_request = client.request

    def request(method, path, **kwargs):
        response = original_request(method, path, **kwargs)
        return httpx.Response(200, content=b"\xff") if path == "/prompt" else response

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(SubmissionUnknown):
        generate(project, client, session, "a cabin", "", 1)
    assert collect(project, client, session).saved == 1
    assert stub.submissions == 1
