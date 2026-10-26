"""Test endpoint and authentication boundaries."""

import base64

import httpx
import pytest
from pydantic import ValidationError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Session
from komaquill.infrastructure.comfyui import ComfyClient


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
    with pytest.raises(KomaquillError):
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
        with pytest.raises(KomaquillError):
            client.json("/history")
    finally:
        client.close()


@pytest.mark.parametrize(
    "definition,expected",
    [
        ([["model.pth"]], ["model.pth"]),
        (["COMBO", {"options": ["model.pth"]}], ["model.pth"]),
        ([], []),
        (["COMBO"], []),
        (["COMBO", "invalid"], []),
        (["OTHER"], []),
    ],
)
def test_model_choices_support_both_standard_node_schemas(definition, expected):
    from komaquill.infrastructure.comfyui import input_choices

    nodes = {"Loader": {"input": {"required": {"model_name": definition}}}}
    assert input_choices(nodes, "Loader", "model_name") == expected


@pytest.mark.parametrize("case", ["plain", "exact", "external", "ambiguous"])
def test_external_model_choices_preserve_actual_names_without_assuming_hashes(case):
    reference = "a" * 64 + "/model.safetensors"
    graph = {
        "4": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "plain.sft" if case == "plain" else reference},
        }
    }
    ui = {"nodes": [{"id": 4, "widgets_values": [graph["4"]["inputs"]["unet_name"]]}]}
    choices = (
        [reference]
        if case == "exact"
        else ["first/model.safetensors", "second/model.safetensors"]
        if case == "ambiguous"
        else ["model.safetensors"]
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"UNETLoader": {"input": {"required": {"unet_name": [choices]}}}}
        )
    )
    with_close = ComfyClient("http://127.0.0.1:8188", transport=transport)
    try:
        if case == "ambiguous":
            with pytest.raises(KomaquillError):
                with_close.resolve_models(graph, ui)
        else:
            api, browser = with_close.resolve_models(graph, ui)
            assert api["4"]["inputs"]["unet_name"] == browser["nodes"][0]["widgets_values"][0]
            assert api["4"]["inputs"]["unet_name"] == (
                "plain.sft"
                if case == "plain"
                else reference
                if case == "exact"
                else "model.safetensors"
            )
    finally:
        with_close.close()
