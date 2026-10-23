"""Test endpoint and authentication boundaries."""

import base64

import httpx
import pytest
from pydantic import ValidationError

from komaquill.client import ComfyClient
from komaquill.errors import KomaquillError
from komaquill.models import Session


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com",
        "https://user:password@example.com",  # pragma: allowlist secret Test URL.
        "https://example.com/?token=secret",
        "file:///tmp/data",
        "https://example.com/other",
    ],
)
def test_unsafe_endpoint_is_rejected(session, endpoint):
    with pytest.raises(ValidationError):
        Session.model_validate({**session.model_dump(), "endpoint": endpoint})


def test_authentication_is_sent_but_error_body_is_hidden():
    password = "test-only-password"  # pragma: allowlist secret Test-only value.

    def respond(request):
        expected = base64.b64encode(f"example-user:{password}".encode()).decode()
        assert request.headers["Authorization"] == "Basic " + expected
        return httpx.Response(401, text=password)

    client = ComfyClient(
        "https://example.com",
        password,
        username="example-user",
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(KomaquillError) as error:
        client.json("/system_stats")
    assert password not in str(error.value)
    client.close()


def test_redirect_does_not_forward_credentials():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://other.example.com"})

    client = ComfyClient(
        "https://example.com",
        "test-only-password",
        username="example-user",
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(KomaquillError):
        client.json("/system_stats")
    assert len(requests) == 1
    client.close()


@pytest.mark.parametrize("queue", [{"queue_running": None}, {"queue_pending": [None]}])
def test_invalid_queue_raises_safe_operation_error(client, stub, queue):
    stub.queue = queue
    with pytest.raises(KomaquillError, match="キュー形式"):
        client.queue()


@pytest.mark.parametrize("endpoint", ["https://example.com:wrong", "https://example.com:70000"])
def test_invalid_endpoint_port_is_rejected(endpoint):
    from uuid import uuid4

    with pytest.raises(ValidationError):
        Session(endpoint=endpoint, runtime_id=uuid4())


def test_non_unicode_json_response_raises_safe_error():
    client = ComfyClient(
        "https://example.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"\xff")),
    )
    try:
        with pytest.raises(KomaquillError, match="応答形式"):
            client.json("/history")
    finally:
        client.close()
