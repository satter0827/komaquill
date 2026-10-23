"""Register a small native ComfyUI API without defining computation nodes."""

import asyncio
import hashlib
import hmac
import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from aiohttp import web
from filelock import FileLock

from komaquill.domain.assets import PREPARATION_TERMINAL, PROTOCOL_VERSION, Preparation
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.domain.presets import MODEL_ROLES
from komaquill.gpu.preservation import Preservation
from komaquill.infrastructure.asset_download import download_asset
from komaquill.infrastructure.asset_store import (
    model_path,
    verified_cache,
)
from komaquill.infrastructure.asset_store import (
    register as register_model,
)
from komaquill.infrastructure.storage import atomic_json
from komaquill.messages import catalog


class Extension:
    """Keep explicit preparation jobs on the existing server and runtime."""

    def __init__(self, root: Path, models: Path, timeout: float) -> None:
        self.root, self.models, self.timeout = root, models, timeout
        self.folder = root / "state/preparation"
        self.tasks: dict[UUID, asyncio.Task[None]] = {}
        self.accepting = True
        self.workspace_id: UUID | None = None
        self.control_token: str | None = None
        self.preservation: Preservation | None = None
        self.ui_clients: set[str] = set()

    def path(self, identity: str | UUID) -> Path:
        """Constrain externally supplied identifiers before filesystem access."""
        return self.folder / f"{UUID(str(identity))}.json"

    def read(self, identity: str | UUID) -> Preparation:
        return Preparation.model_validate_json(self.path(identity).read_bytes())

    def update(self, identity: str | UUID, **values: Any) -> Preparation:
        """Publish monotonic cancellation and verified completion under one lock."""
        path = self.path(identity)
        with FileLock(str(path) + ".lock"):
            current = self.read(identity)
            if current.phase == "cancelled":
                return current
            if current.phase == "ready":
                return current
            if values.get("phase", current.phase) != current.phase:
                values["phase_started_at"] = datetime.now(UTC)
            current = Preparation.model_validate(
                {**current.model_dump(), **values, "updated_at": datetime.now(UTC)}
            )
            atomic_json(path, current.model_dump(mode="json"))
            return current

    async def prepare(self, identity: UUID) -> None:
        """Place confirmed remote models without waiting on the server event loop."""
        try:
            job = self.read(identity)
            self.update(identity, phase="preparing")
            for model in job.assets:
                self.update(identity, current_asset=model.reference)
                if model.local_only:
                    continue
                await asyncio.to_thread(
                    download_asset,
                    self.models,
                    model,
                    self.timeout,
                    lambda: self.read(identity).phase == "cancelled" or not self.accepting,
                )
            current = self.read(identity)
            all_ready = all(verified_cache(self.models, model) for model in current.assets)
            self.update(
                identity,
                remote_ready=all_ready,
                current_asset=None,
                phase="ready" if all_ready and current.local_saved else "preparing",
            )
        except Exception as error:
            self.update(identity, phase="failed", failure=failure_info(error, "preparation"))

    async def upload(self, request: web.Request) -> web.Response:
        if not self.authorized(request) or not self.accepting:
            return web.json_response({"code": "authentication_failed"}, status=403)
        job = self.read(request.match_info["identity"])
        model = next(
            (a for a in job.assets if a.sha256 == request.match_info["sha256"] and a.local_only),
            None,
        )
        if model is None or job.phase not in {"confirmed", "preparing"}:
            return web.json_response({"code": "invalid_request"}, status=409)
        path = model_path(self.models, model)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.upload")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as handle:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    if not self.accepting or self.read(job.request_id).phase == "cancelled":
                        raise KomaquillError(code="stopping")
                    size += len(chunk)
                    if model.size_bytes is not None and size > model.size_bytes:
                        raise KomaquillError(code="invalid_output")
                    digest.update(chunk)
                    handle.write(chunk)
            if digest.hexdigest() != model.sha256:
                raise KomaquillError(code="invalid_output")
            temporary.replace(path)
            register_model(self.models, model, path)
        finally:
            temporary.unlink(missing_ok=True)
        current = self.read(job.request_id)
        ready = all(verified_cache(self.models, a) for a in current.assets)
        self.update(
            job.request_id,
            remote_ready=ready,
            phase="ready" if ready and current.local_saved else "preparing",
        )
        return web.json_response(self.read(job.request_id).model_dump(mode="json"))

    async def capabilities(self, request: web.Request) -> web.Response:
        client_id = request.query.get("client_id")
        if client_id and len(client_id) <= 128:
            self.ui_clients.add(client_id)
        return web.json_response(
            {
                "protocol_version": PROTOCOL_VERSION,
                "workspace_id": str(self.workspace_id) if self.workspace_id else None,
                "accepting": self.accepting,
                "messages": {k: v for k, v in catalog().items() if k.startswith("ui.")},
            }
        )

    async def jobs(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "requests": [
                    self.read(p.stem).model_dump(mode="json") for p in self.folder.glob("*.json")
                ]
            }
        )

    async def create(self, request: web.Request) -> web.Response:
        if not self.accepting or self.workspace_id is None:
            return web.json_response({"code": "stopping"}, status=409)
        data = await request.json()
        job = Preparation(workspace_id=self.workspace_id, workflow=data["workflow"])
        atomic_json(self.path(job.request_id), job.model_dump(mode="json"))
        return web.json_response(job.model_dump(mode="json"))

    async def action(self, request: web.Request) -> web.Response:
        identity = UUID(request.match_info["identity"])
        data = await request.json()
        action = data.get("action")
        if action in {"plan", "local_saved", "failure"} and not self.authorized(request):
            return web.json_response({"code": "authentication_failed"}, status=403)
        current = self.read(identity)
        if action == "cancel":
            current = self.update(identity, phase="cancelled")
        elif (
            action == "confirm"
            and current.phase == "confirmation"
            and not current.unsupported
            and self.accepting
        ):
            current = self.update(identity, phase="confirmed")
            self.tasks[identity] = asyncio.create_task(self.prepare(identity))
        elif action == "plan" and current.phase == "requested":
            current = self.update(
                identity,
                phase="confirmation",
                assets=data["assets"],
                reused=data["reused"],
                unsupported=data["unsupported"],
            )
        elif action == "local_saved" and current.phase in {"confirmed", "preparing"}:
            current = self.update(
                identity, local_saved=True, phase="ready" if current.remote_ready else current.phase
            )
        elif action == "failure" and current.phase not in PREPARATION_TERMINAL:
            current = self.update(identity, phase="failed", failure=data["failure"])
        else:
            return web.json_response({"code": "invalid_request"}, status=409)
        return web.json_response(current.model_dump(mode="json"))

    async def attach(self, request: web.Request) -> web.Response:
        data = await request.json()
        identity = UUID(data["workspace_id"])
        token = request.headers.get("X-Komaquill-Control", "")
        if len(token) < 32 or (
            self.control_token and not hmac.compare_digest(token, self.control_token)
        ):
            return web.json_response({"code": "authentication_failed"}, status=403)
        if self.workspace_id is not None and identity != self.workspace_id:
            return web.json_response({"code": "identity_mismatch"}, status=409)
        self.control_token = token
        self.workspace_id = identity
        self.accepting = data.get("accepting", True) is True
        if not self.accepting:
            for path in self.folder.glob("*.json"):
                if self.read(path.stem).phase not in PREPARATION_TERMINAL:
                    self.update(path.stem, phase="cancelled")
        if not self.accepting and self.preservation is not None:
            async with self.preservation.lock:
                pass
        return web.json_response({"workspace_id": str(identity)})

    def authorized(self, request: web.Request) -> bool:
        return bool(
            self.control_token
            and hmac.compare_digest(
                request.headers.get("X-Komaquill-Control", ""), self.control_token
            )
        )

    async def guarded(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """Return safe validation errors without exposing server files or response text."""
        try:
            return cast(web.StreamResponse, await handler(request))
        except (ValueError, KeyError, TypeError, OSError, KomaquillError):
            return web.json_response({"code": "invalid_request"}, status=400)

    def routes(self) -> web.RouteTableDef:
        routes = web.RouteTableDef()
        for method, path, handler in [
            ("GET", "/komaquill/v1/capabilities", self.capabilities),
            ("GET", "/komaquill/v1/requests", self.jobs),
            ("POST", "/komaquill/v1/requests", self.create),
            ("POST", "/komaquill/v1/requests/{identity}", self.action),
            ("POST", "/komaquill/v1/requests/{identity}/upload/{sha256}", self.upload),
            ("POST", "/komaquill/v1/monitor", self.attach),
        ]:

            async def wrapped(request: web.Request, bound: Any = handler) -> web.StreamResponse:
                return await self.guarded(request, bound)

            routes.route(method, path)(wrapped)
        return routes


def register(server: Any, root: Path, timeout: float) -> Extension:
    """Install on PromptServer before it freezes its routes."""
    extension = Extension(root, root, timeout)
    folders = importlib.import_module("folder_paths")
    for role in MODEL_ROLES:
        destination = root / "models" / role
        destination.mkdir(parents=True, exist_ok=True)
        folders.add_model_folder_path(role, str(destination))
    for route in extension.routes():
        if isinstance(route, web.RouteDef):
            server.routes.route(route.method, route.path)(route.handler)
    extension.preservation = Preservation(server, extension, Path(folders.get_input_directory()))
    extension.preservation.install()
    return extension
