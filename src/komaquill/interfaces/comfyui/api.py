"""Register a small native ComfyUI API without defining computation nodes."""

import asyncio
import hmac
import importlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from aiohttp import web
from filelock import FileLock

from komaquill.domain.assets import PREPARATION_TERMINAL, PROTOCOL_VERSION, Preparation
from komaquill.domain.errors import FailureInfo, KomaquillError, failure_info
from komaquill.domain.presets import MODEL_ROLES
from komaquill.infrastructure.asset_download import download_asset
from komaquill.infrastructure.asset_store import (
    verified_cache,
)
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.paths import GpuPaths
from komaquill.interfaces.comfyui.preservation import Preservation
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
        deadline = os.environ.get("KOMAQUILL_SUBMIT_UNTIL")
        self.submit_until = float(deadline) if deadline is not None else None

    @property
    def accepting(self) -> bool:
        """Enforce the frozen submission deadline even while local review is waiting."""
        return self._accepting and (self.submit_until is None or time.time() < self.submit_until)

    @accepting.setter
    def accepting(self, value: bool) -> None:
        self._accepting = value

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
            if current.phase in PREPARATION_TERMINAL:
                return current
            if values.get("phase", current.phase) != current.phase:
                values["phase_started_at"] = datetime.now(UTC)
            current = Preparation.model_validate(
                {**current.model_dump(), **values, "updated_at": datetime.now(UTC)}
            )
            atomic_json(path, current.model_dump(mode="json"))
            return current

    async def prepare(self, identity: UUID, credentials: dict[str, str] | None = None) -> None:
        """Place confirmed remote models without waiting on the server event loop."""
        try:
            job = self.read(identity)
            if job.phase not in {"confirmed", "preparing"}:
                return
            self.update(identity, phase="preparing")
            for model in job.assets:
                self.update(identity, current_asset=model.reference)

                def report_retry(failure: FailureInfo, name: str = model.name) -> None:
                    record_event(
                        GpuPaths(self.root.parent),
                        "model",
                        "download_retrying",
                        asset=name,
                        operation_id=identity,
                        failure=failure,
                    )

                await asyncio.to_thread(
                    download_asset,
                    self.models,
                    model,
                    self.timeout,
                    lambda: self.read(identity).phase == "cancelled" or not self.accepting,
                    credentials,
                    report_retry,
                )
            current = self.read(identity)
            all_ready = all(verified_cache(self.models, model) for model in current.assets)
            self.update(
                identity,
                remote_ready=all_ready,
                current_asset=None,
                phase="ready" if all_ready else "failed",
            )
        except Exception as error:
            self.update(identity, phase="failed", failure=failure_info(error, "preparation"))

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
        if action in {"plan", "failure"} and not self.authorized(request):
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
        elif action == "plan" and current.phase == "requested":
            current = self.update(
                identity,
                phase="confirmation",
                assets=data["assets"],
                node_models=data.get("node_models", {}),
                reused=data["reused"],
                unsupported=data["unsupported"],
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
            ("POST", "/komaquill/v1/monitor", self.attach),
        ]:

            async def wrapped(request: web.Request, bound: Any = handler) -> web.StreamResponse:
                return await self.guarded(request, bound)

            routes.route(method, path)(wrapped)
        return routes


def register(server: Any, root: Path, timeout: float) -> Extension:
    """Install on PromptServer before it freezes its routes."""
    extension = Extension(root, root / "models", timeout)
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
