"""Observe standard ComfyUI uploads and workflow saves on the existing server."""

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from aiohttp import web

from komaquill.domain.errors import failure_info
from komaquill.domain.materials import Material
from komaquill.infrastructure.storage import atomic_json

CHUNK_BYTES = 1024 * 1024
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


class Preservation:
    """Archive only observed standard writes and inputs used by new requests."""

    def __init__(self, server: Any, control: Any, input_root: Path) -> None:
        self.server, self.control, self.input_root = server, control, input_root
        self.folder = control.root / "state/preservation"
        self.blobs = control.root / "cache/preservation"
        self.lock = asyncio.Lock()

    def emit(self, payload: dict[str, Any]) -> None:
        """Notify only connected clients that have loaded the workspace extension."""
        self.control.ui_clients.intersection_update(self.server.sockets)
        for client_id in self.control.ui_clients:
            self.server.send_sync("komaquill_saved", payload, client_id)

    def rows(self) -> list[Material]:
        rows = []
        self.errors = []
        for path in self.folder.glob("*.json"):
            try:
                rows.append(Material.model_validate_json(path.read_bytes()))
            except (ValueError, OSError):
                try:
                    identity = str(UUID(path.stem))
                except ValueError:
                    identity = "invalid-metadata"
                self.errors.append(identity)
        return rows

    def snapshot(
        self, source: Path, kind: str, name: str, reference: str | None = None
    ) -> Material:
        """Hash and copy the same stream before publishing its immutable revision."""
        self.blobs.mkdir(parents=True, exist_ok=True)
        temporary = self.blobs / f"{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as origin, temporary.open("xb") as target:
                while chunk := origin.read(CHUNK_BYTES):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            sha = digest.hexdigest()
            target = self.blobs / sha
            temporary.replace(target)
            bindings = {}
            if kind == "workflows":
                graph = json.loads(target.read_bytes())
                for node in graph.get("nodes", []):
                    if node.get("type") not in {"LoadImage", "LoadImageMask"}:
                        continue
                    values = node.get("widgets_values", [])
                    if not values or not isinstance(values[0], str):
                        continue
                    relative = values[0].removesuffix(" [input]")
                    source_input = (self.input_root / relative).resolve()
                    if not source_input.is_relative_to(self.input_root.resolve()):
                        raise ValueError
                    captured = self.snapshot(source_input, "inputs", relative)
                    bindings[relative] = captured.sha256
            material = Material.model_validate(
                {
                    "workspace_id": self.control.workspace_id,
                    "runtime_id": os.environ.get("KOMAQUILL_RUNTIME_ID"),
                    "kind": kind,
                    "name": name,
                    "reference": reference or name,
                    "sha256": sha,
                    "size_bytes": size,
                    "input_sha256": bindings,
                }
            )
            atomic_json(
                self.folder / f"{material.material_id}.json", material.model_dump(mode="json")
            )
            return material
        finally:
            temporary.unlink(missing_ok=True)

    def pin_inputs(self, data: dict[str, Any]) -> dict[str, Any]:
        """Keep queued LoadImage inputs independent of later uploads with the same name."""
        if self.control.workspace_id is None:
            return data
        try:
            bindings = {}
            for node in data.get("prompt", {}).values():
                if node.get("class_type") not in {"LoadImage", "LoadImageMask"}:
                    continue
                value = node["inputs"].get("image")
                if not isinstance(value, str):
                    continue
                relative = value.removesuffix(" [input]")
                source = (self.input_root / relative).resolve()
                if not source.is_relative_to(self.input_root.resolve()) or not source.is_file():
                    raise ValueError
                material = self.snapshot(source, "inputs", relative)
                reference = f"komaquill/{material.sha256}/{source.name}"
                immutable = self.input_root / reference
                immutable.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.blobs / material.sha256, immutable)
                node["inputs"]["image"] = reference
                bindings[reference] = material.sha256
            if bindings:
                data.setdefault("extra_data", {}).setdefault("komaquill", {})["input_sha256"] = (
                    bindings
                )
        except Exception as error:
            # PromptServer continues after hook errors, so return its standard rejection shape.
            data.pop("prompt", None)
            self.emit(
                {
                    "state": "retry",
                    "failure": failure_info(error, "storage").model_dump(mode="json"),
                },
            )
        return data

    def middleware(self) -> Any:
        @web.middleware
        async def observe(request: web.Request, handler: Any) -> web.StreamResponse:
            path = request.path.removeprefix("/api")
            tracked = request.method == "POST" and (
                path in {"/upload/image", "/upload/mask"} or path.startswith("/userdata/")
            )
            prompt = path == "/prompt" and request.method == "POST"
            if (
                (tracked or prompt)
                and self.control.workspace_id is not None
                and not self.control.accepting
            ):
                return web.json_response({"code": "stopping"}, status=409)
            if (
                not (tracked or prompt)
                or self.control.workspace_id is None
                or self.control.authorized(request)
            ):
                return cast(web.StreamResponse, await handler(request))
            async with self.lock:
                response: web.StreamResponse = await handler(request)
                if response.status >= 300 or prompt:
                    return response
                try:
                    if path in {"/upload/image", "/upload/mask"}:
                        payload = json.loads(cast(bytes, cast(web.Response, response).body))
                        if payload.get("type") != "input":
                            return response
                        name = str(Path(payload.get("subfolder", "")) / payload["name"]).replace(
                            "\\", "/"
                        )
                        source = (self.input_root / name).resolve()
                        if (
                            not source.is_relative_to(self.input_root.resolve())
                            or source.suffix.lower() not in IMAGE_SUFFIXES
                        ):
                            return response
                        await asyncio.to_thread(self.snapshot, source, "inputs", name)
                    else:
                        name = request.match_info.get("file", "")
                        if name.startswith("workflows/") and name.endswith(".json"):
                            source = Path(
                                self.server.user_manager.get_request_user_filepath(
                                    request, name, create_dir=False
                                )
                            )
                            await asyncio.to_thread(self.snapshot, source, "workflows", name)
                except Exception as error:
                    self.emit(
                        {
                            "state": "retry",
                            "failure": failure_info(error, "storage").model_dump(mode="json"),
                        },
                    )
                return response

        return observe

    async def index(self, request: web.Request) -> web.Response:
        rows = self.rows()
        return web.json_response(
            {
                "files": [
                    r.model_dump(mode="json")
                    for r in rows
                    if r.workspace_id == self.control.workspace_id
                ],
                "errors": self.errors,
            }
        )

    async def content(self, request: web.Request) -> web.StreamResponse:
        identity = UUID(request.match_info["identity"])
        row = Material.model_validate_json((self.folder / f"{identity}.json").read_bytes())
        return web.FileResponse(self.blobs / row.sha256)

    async def acknowledge(self, request: web.Request) -> web.Response:
        if not self.control.authorized(request):
            return web.json_response({"code": "authentication_failed"}, status=403)
        identity = UUID(request.match_info["identity"])
        path = self.folder / f"{identity}.json"
        row = Material.model_validate_json(path.read_bytes())
        data = await request.json()
        if data.get("sha256") != row.sha256:
            return web.json_response({"code": "identity_mismatch"}, status=409)
        row.state = "saved"
        atomic_json(path, row.model_dump(mode="json"))
        self.emit({"state": "saved", "material_id": str(identity), "kind": row.kind})
        return web.json_response(row.model_dump(mode="json"))

    async def notify(self, request: web.Request) -> web.Response:
        if not self.control.authorized(request):
            return web.json_response({"code": "authentication_failed"}, status=403)
        data = await request.json()
        state = data.get("state")
        if state not in {"saving", "saved", "retry"}:
            return web.json_response({"code": "invalid_request"}, status=400)
        self.emit({"state": state, "prompt_id": str(data.get("prompt_id", ""))})
        return web.json_response({"state": state})

    def install(self) -> None:
        self.server.app.middlewares.append(self.middleware())
        self.server.add_on_prompt_handler(self.pin_inputs)
        for method, path, handler in [
            ("GET", "/komaquill/v1/files", self.index),
            ("GET", "/komaquill/v1/files/{identity}", self.content),
            ("POST", "/komaquill/v1/files/{identity}", self.acknowledge),
            ("POST", "/komaquill/v1/save-status", self.notify),
        ]:

            async def wrapped(request: web.Request, bound: Any = handler) -> web.StreamResponse:
                return cast(web.StreamResponse, await self.control.guarded(request, bound))

            self.server.routes.route(method, path)(wrapped)
