"""Coordinate public model preparation through an attached native extension."""

import contextlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import keyring
from filelock import FileLock

from komaquill.application.preparation import requirements
from komaquill.domain.assets import PREPARATION_TERMINAL, Preparation
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.infrastructure.asset_download import download_asset
from komaquill.infrastructure.asset_store import inventory, model_path, verified_cache
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.model_sources import resolve_model
from komaquill.infrastructure.storage import atomic_json, load_session
from komaquill.messages import message

PREFIX = "/komaquill/v1"


def capabilities(client: ComfyClient) -> bool:
    """Leave bare ComfyUI endpoints on the standard generation API."""
    try:
        return client.json(PREFIX + "/capabilities").get("protocol_version") == 1
    except KomaquillError as error:
        if error.http_status == 404:
            return False
        raise


def verify_extension(client: ComfyClient) -> None:
    """Require the installed extension before claiming a managed session is ready."""
    if not capabilities(client):
        raise KomaquillError(code="extension_unavailable", stage="preparation", retryable=False)


class Coordinator:
    """Run bounded preparation jobs separately from result collection."""

    def __init__(self, project: Project, client: ComfyClient) -> None:
        self.project, self.client = project, client
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preparation")
        self.futures: dict[UUID, Future[None]] = {}
        self.cancel_events: dict[UUID, threading.Event] = {}
        self.bound: set[UUID] = set()
        path = project.state / "workspace.json"
        self.workspace_id = (
            UUID(json.loads(path.read_bytes())["workspace_id"]) if path.is_file() else uuid4()
        )
        atomic_json(path, {"workspace_id": str(self.workspace_id)})
        key = str(project.root)
        token = keyring.get_password("komaquill-extension", key)
        if token is None:
            token = secrets.token_urlsafe(32)
            keyring.set_password("komaquill-extension", key, token)
        self.headers = {"X-Komaquill-Control": token}
        self.client.request(
            "POST",
            PREFIX + "/monitor",
            headers=self.headers,
            json={"workspace_id": str(self.workspace_id), "accepting": True},
        )

    def update(self, job: Preparation, action: str, **data: object) -> Preparation:
        response = self.client.request(
            "POST",
            PREFIX + f"/requests/{job.request_id}",
            headers=self.headers,
            json={"action": action, **data},
        )
        return Preparation.model_validate(response.json())

    def plan(self, job: Preparation) -> None:
        hints, unsupported = requirements(job.workflow, self.client.json("/object_info"))
        assets = []
        for hint in hints:
            try:
                candidates = [
                    m
                    for m in inventory(self.project.artifacts)
                    if m.filename == hint["name"] and m.role == hint["directory"]
                ]
                asset = (
                    candidates[0]
                    if len(candidates) == 1 and not hint.get("url")
                    else resolve_model(
                        hint["name"],
                        hint["directory"],
                        hint["url"],
                        self.project.settings().request_timeout,
                    )
                )
                if asset not in assets:
                    assets.append(asset)
            except (KomaquillError, KeyError):
                unsupported.append(str(hint.get("name", "unknown")))
        self.update(
            job,
            "plan",
            assets=[a.model_dump(mode="json") for a in assets],
            unsupported=sorted(set(unsupported)),
            reused=[a.reference for a in assets if verified_cache(self.project.artifacts, a)],
        )

    def run(self, job: Preparation) -> None:
        """Preserve local bytes while the server downloads from the same fixed sources."""
        try:
            if job.phase == "requested":
                self.plan(job)
                return
            for asset in job.assets:
                download_asset(
                    self.project.artifacts,
                    asset,
                    self.project.settings().request_timeout,
                    lambda: (
                        self.cancelled.is_set()
                        or self.cancel_events.setdefault(job.request_id, threading.Event()).is_set()
                    ),
                )
                if asset.local_only:
                    path = model_path(self.project.artifacts, asset)
                    with path.open("rb") as handle:
                        self.client.request(
                            "POST",
                            PREFIX + f"/requests/{job.request_id}/upload/{asset.sha256}",
                            headers={**self.headers, "Content-Length": str(path.stat().st_size)},
                            content=handle,
                        )

            self.update(job, "local_saved")
            record_event(self.project, "preparation", "local_saved", operation_id=job.request_id)
        except Exception as error:
            record_event(
                self.project,
                "preparation",
                "failed",
                operation_id=job.request_id,
                failure=failure_info(error, "preparation"),
            )
            if not self.cancelled.is_set():
                with contextlib.suppress(KomaquillError):
                    self.update(
                        job,
                        "failure",
                        failure=failure_info(error, "preparation").model_dump(mode="json"),
                    )

    def tick(self) -> None:
        """Schedule jobs without making large transfers on the collection loop."""
        data = self.client.json(PREFIX + "/requests")
        for item in data["requests"]:
            job = Preparation.model_validate(item)
            if job.workspace_id != self.workspace_id:
                continue
            local = self.project.state / "preparation" / f"{job.request_id}.json"
            if not local.is_file() or Preparation.model_validate_json(local.read_bytes()) != job:
                atomic_json(local, job.model_dump(mode="json"))
            if job.phase in {"cancelled", "failed"}:
                self.cancel_events.setdefault(job.request_id, threading.Event()).set()
            if job.phase == "ready" and job.request_id not in self.bound:
                self.bind(job)
            future = self.futures.get(job.request_id)
            if (
                job.phase in {"requested", "confirmed", "preparing"}
                and not job.local_saved
                and (future is None or future.done())
            ):
                self.futures[job.request_id] = self.executor.submit(self.run, job)

    def bind(self, job: Preparation) -> None:
        """Associate remote-verified weights with future generation records."""
        save_bindings(self.project, job)
        self.bound.add(job.request_id)

    def close(self) -> None:
        """Stop accepting preparation and cancel in-flight model transfers."""
        self.cancelled.set()
        try:
            self.client.request(
                "POST",
                PREFIX + "/monitor",
                headers=self.headers,
                json={"workspace_id": str(self.workspace_id), "accepting": False},
            )
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)


def save_bindings(project: Project, job: Preparation) -> None:
    """Publish only a completed plan for the connected runtime."""
    if not project.session_file.is_file():
        return
    session = load_session(project)
    path = project.state / "model-bindings.json"
    with FileLock(str(path) + ".lock"):
        previous = json.loads(path.read_bytes()) if path.is_file() else {}
        assets = (
            previous.get("assets", {})
            if previous.get("runtime_id") == str(session.runtime_id)
            else {}
        )
        assets.update({asset.reference: asset.model_dump(mode="json") for asset in job.assets})
        atomic_json(
            path, {"format_version": 1, "runtime_id": str(session.runtime_id), "assets": assets}
        )


def prepare_workflow(
    project: Project,
    client: ComfyClient,
    workflow: Path,
    echo: Callable[[str], None],
    confirm: Callable[[str], bool],
) -> None:
    """Confirm one explicit model plan; never allocate or restart a GPU."""
    if not capabilities(client):
        raise KomaquillError(code="extension_unavailable", retryable=False)
    content = json.loads(workflow.read_bytes())
    response = client.request("POST", PREFIX + "/requests", json={"workflow": content})
    job = Preparation.model_validate(response.json())
    deadline = time.monotonic() + project.settings().preparation_timeout
    previous = None
    try:
        while job.phase not in PREPARATION_TERMINAL:
            if time.monotonic() >= deadline:
                raise KomaquillError(code="operation_failed", stage="preparation")
            if previous != job.phase:
                echo(message("assets.phase", request_id=job.request_id, phase=job.phase))
                previous = job.phase
            if job.phase == "confirmation":
                if job.unsupported:
                    echo(message("assets.unsupported", items=", ".join(job.unsupported)))
                    raise KomaquillError(code="unsupported_asset", retryable=False)
                for asset in job.assets:
                    echo(
                        message(
                            "assets.row",
                            name=asset.filename,
                            size=str(asset.size_bytes)
                            if asset.size_bytes is not None
                            else message("ui.unknown_size"),
                            source=asset.url or asset.repo_id,
                            reused=str(asset.reference in job.reused),
                        )
                    )
                action = "confirm" if confirm(message("assets.confirm")) else "cancel"
                job = Preparation.model_validate(
                    client.request(
                        "POST", PREFIX + f"/requests/{job.request_id}", json={"action": action}
                    ).json()
                )
            time.sleep(project.settings().poll_interval)
            rows = client.json(PREFIX + "/requests")["requests"]
            job = next(
                Preparation.model_validate(r)
                for r in rows
                if r["request_id"] == str(job.request_id)
            )
    except (Exception, KeyboardInterrupt):
        with contextlib.suppress(KomaquillError):
            client.request(
                "POST", PREFIX + f"/requests/{job.request_id}", json={"action": "cancel"}
            )
        raise
    if job.phase != "ready":
        raise KomaquillError(
            code=job.failure.code if job.failure else "stopping", stage="preparation"
        )
    save_bindings(project, job)
    echo(message("assets.ready"))
