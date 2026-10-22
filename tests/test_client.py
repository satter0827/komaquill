"""認証情報と接続先の境界を検証する。"""

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
        "https://user:password@example.com",  # pragma: allowlist secret 試験用の認証文字列
        "https://example.com/?token=secret",
        "file:///tmp/data",
        "https://example.com/other",
    ],
)
def test_unsafe_endpoint_is_rejected(session, endpoint):
    with pytest.raises(ValidationError):
        Session.model_validate({**session.model_dump(), "endpoint": endpoint})


def test_authentication_is_sent_but_error_body_is_hidden():
    password = "test-only-password"  # pragma: allowlist secret 試験用の値

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
