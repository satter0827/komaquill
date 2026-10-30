"""Access the standard ComfyUI HTTP API."""

import hashlib
from json import JSONDecodeError
from typing import Any, cast
from urllib.parse import quote

import httpx

from komaquill.domain.errors import KomaquillError, SubmissionUnknown
from komaquill.domain.materials import InputArtifact
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

    def resolve_models(self, graph: JsonObject, ui: JsonObject) -> tuple[JsonObject, JsonObject]:
        """Use the external server's standard model choices without claiming their hashes."""
        import re
        from pathlib import PurePosixPath

        references = {
            value
            for node in graph.values()
            if isinstance(node, dict)
            for key, value in node.get("inputs", {}).items()
            if key.endswith(("_name", "_name1", "_name2", "_name3", "_file"))
            and isinstance(value, str)
        }
        if not any(re.match(r"^[a-f0-9]{64}/", value) for value in references):
            return graph, ui
        definitions = self.json("/object_info")
        changes: dict[str, dict[str, str]] = {}
        for identity, node in graph.items():
            for key, value in node.get("inputs", {}).items():
                if not isinstance(value, str) or value not in references:
                    continue
                choices = input_choices(definitions, node.get("class_type", ""), key)
                if not choices or value in choices:
                    continue
                candidates = [
                    name
                    for name in choices
                    if PurePosixPath(name).name == PurePosixPath(value).name
                ]
                if len(candidates) != 1:
                    raise KomaquillError(
                        code="invalid_request",
                        display_key="text.the_selected_model_is_missing_check_the_comfyui",
                    )
                node["inputs"][key] = candidates[0]
                changes.setdefault(str(identity), {})[value] = candidates[0]
        for node in ui.get("nodes", []):
            replacements = changes.get(str(node["id"]), {})
            node["widgets_values"] = [
                replacements.get(value, value) if isinstance(value, str) else value
                for value in node.get("widgets_values", [])
            ]
        return graph, ui

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

    def upload_image(self, item: InputArtifact, content: bytes) -> None:
        """Use immutable content-addressed standard inputs and verify remote bytes."""
        from pathlib import PurePosixPath

        reference = PurePosixPath(item.reference)
        folder = str(reference.parent) if len(reference.parts) > 1 else ""
        filename = reference.name
        response = self.request(
            "POST",
            "/upload/image",
            files={"image": (filename, content)},
            data={"subfolder": folder, "type": "input", "overwrite": "true"},
        )
        try:
            value = response.json()
            if (
                value["name"] != filename
                or value["subfolder"] != folder
                or value["type"] != "input"
            ):
                raise ValueError
            returned = self.request(
                "GET", "/view", params={"filename": filename, "subfolder": folder, "type": "input"}
            ).content
            if (
                len(returned) != item.size_bytes
                or hashlib.sha256(returned).hexdigest() != item.sha256
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise KomaquillError(
                code="invalid_output", stage="preparation", retryable=False
            ) from None


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
