"""Access the standard ComfyUI HTTP API."""

from json import JSONDecodeError
from typing import Any, cast
from urllib.parse import quote

import httpx

from komaquill.domain.errors import KomaquillError, SubmissionUnknown
from komaquill.domain.models import ImageRef, JsonObject


class ComfyClient:
    """Send credentials only to the selected endpoint without redirects."""

    def __init__(
        self,
        endpoint: str,
        password: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        *,
        username: str | None = None,
    ) -> None:
        self.http = httpx.Client(
            base_url=endpoint,
            auth=httpx.BasicAuth(username or "komaquill", password)
            if password is not None
            else None,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Classify failures without exposing response bodies or credentials."""
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            if method == "POST" and path == "/prompt":
                raise SubmissionUnknown(
                    display_key="text.submission_is_unconfirmed_run_collect_to_reconcile_the"
                ) from exc
            raise KomaquillError(
                display_key="text.cannot_connect_to_comfyui_check_the_endpoint_and",
                code="connection_failed",
                stage="communication",
            ) from exc
        if response.status_code == 401:
            raise KomaquillError(
                display_key="text.authentication_failed_run_start_to_update_the_credentials",
                code="authentication_failed",
                stage="communication",
                http_status=401,
                retryable=False,
            )
        if method == "POST" and path == "/prompt" and response.status_code >= 500:
            raise SubmissionUnknown(
                display_key="text.submission_is_unconfirmed_run_collect_to_reconcile_the"
            )
        if not response.is_success:
            raise KomaquillError(
                display_key="text.comfyui_operation_failed_http_status_code",
                details={"status_code": str(response.status_code)},
                code="http_failed",
                stage="communication",
                http_status=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        return response

    def json(self, path: str) -> JsonObject:
        """Read an object response."""
        try:
            data = self.request("GET", path).json()
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise KomaquillError(
                display_key="text.comfyui_returned_an_invalid_response",
                code="invalid_response",
                stage="communication",
            ) from exc
        if not isinstance(data, dict):
            raise KomaquillError(
                display_key="text.comfyui_returned_an_invalid_response",
                code="invalid_response",
                stage="communication",
            )
        return cast(JsonObject, data)

    def queue(self) -> list[list[Any]]:
        """Validate standard queue rows without including response bodies in errors."""
        data = self.json("/queue")
        result: list[list[Any]] = []
        for key in ("queue_running", "queue_pending"):
            rows = data.get(key, [])
            if not isinstance(rows, list):
                raise KomaquillError(display_key="text.comfyui_returned_invalid_queue_data")
            for row in rows:
                if not isinstance(row, list) or len(row) < 3 or not isinstance(row[1], str):
                    raise KomaquillError(display_key="text.comfyui_returned_invalid_queue_data")
                result.append(row)
        return result

    def history(self) -> JsonObject:
        """Read the current server history."""
        return self.json("/history")

    def runtime(self) -> JsonObject:
        """Read managed runtime metadata from standard userdata."""
        return self.json("/userdata/" + quote("komaquill/session.json", safe=""))

    def submit(self, graph: JsonObject, ui: JsonObject, run_id: str) -> str:
        """Submit a request and distinguish an unknown acceptance result."""
        response = self.request(
            "POST",
            "/prompt",
            json={
                "prompt": graph,
                "client_id": run_id,
                "extra_data": {"komaquill": {"run_id": run_id}, "extra_pnginfo": {"workflow": ui}},
            },
        )
        try:
            data = response.json()
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise SubmissionUnknown(
                display_key="text.the_prompt_id_is_unconfirmed_run_collect_to"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("prompt_id"), str):
            raise SubmissionUnknown(display_key="text.the_prompt_id_is_unconfirmed_run_collect_to")
        return cast(str, data["prompt_id"])

    def image(self, reference: ImageRef) -> bytes:
        """Download an image referenced by the server history."""
        return self.request("GET", "/view", params=reference.model_dump()).content

    def checkpoints(self) -> list[str]:
        """Validate checkpoint choices without exposing the remote payload."""
        nodes = self.json("/object_info")
        try:
            values = nodes["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise KomaquillError(
                display_key="error.invalid_response",
                code="invalid_response",
                stage="response_validation",
                retryable=False,
            ) from None
        return cast(list[str], values)


def input_choices(nodes: JsonObject, loader: str, field: str) -> list[str]:
    """Read V1 list choices and V3 COMBO options from object_info."""
    definition = nodes.get(loader, {}).get("input", {}).get("required", {}).get(field, [])
    if not isinstance(definition, list) or not definition:
        return []
    options = definition[0]
    if options == "COMBO":
        options = (
            definition[1].get("options", [])
            if len(definition) > 1 and isinstance(definition[1], dict)
            else []
        )
    return [item for item in options if isinstance(item, str)] if isinstance(options, list) else []
