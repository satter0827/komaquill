"""Expose local-only HTTP routes over the same public client used by the CLI."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import BackgroundTasks, Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from komaquill.adapters.diagnostics import record_event
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.web_configuration import WebSettings, defaults
from komaquill.api import Client, KomaquillError, Run, SessionStatus
from komaquill.domain.availability import Availability, AvailabilityRequest
from komaquill.domain.conversations.actions import ActionRecord, ProposalDismissal
from komaquill.domain.conversations.editing import Bookmark, NotesRequest, SceneRequest
from komaquill.domain.conversations.models import (
    AcceptedInput,
    Conversation,
    ConversationInput,
    Operation,
    SendInput,
    Turn,
)
from komaquill.domain.errors import OperationStage, failure_info
from komaquill.domain.materials import InputArtifact
from komaquill.interfaces.http.background import execute_retained
from komaquill.resources.messages import error_message, message


class TurnPage(BaseModel):
    items: list[Turn]
    next_before: int | None


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    prompt: str = Field(min_length=1, max_length=20000)
    seed: int | None = Field(default=None, ge=0)
    model: str | None = None
    llm_profile: str = "fake"
    allow_real_llm: bool = False


class RuntimeStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["external", "colab"] = "external"
    endpoint: str | None = None


class ProfileView(BaseModel):
    id: str
    provider: str
    model: str


class SettingsView(BaseModel):
    profiles: list[ProfileView]
    sample_input: str


class OperationView(Operation):
    input: AcceptedInput | None = None
    error_message: str | None = None
    image_error_message: str | None = None


class ErrorView(BaseModel):
    code: str
    stage: OperationStage
    message: str


def describe_operation(value: Operation, accepted: AcceptedInput | None) -> OperationView:
    def describe(code: str | None) -> str | None:
        if code is None:
            return None
        try:
            failure = KomaquillError(code=code)
        except ValueError:
            failure = KomaquillError(code="operation_failed")
        return error_message(failure)

    return OperationView(
        **value.model_dump(),
        input=accepted,
        error_message=describe(value.error),
        image_error_message=describe(value.image_error),
    )


def create_app(client: Client | None = None, *, ui_directory: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if client is not None:
            client.configure()
            client.operations.reconcile()
            client.library.reconcile()
        yield
        if client is not None:
            client.close()

    app = FastAPI(
        title="Komaquill API",
        version="1.0.0",
        lifespan=lifespan,
        responses={status: {"model": ErrorView} for status in (400, 403, 404, 409, 422, 500)},
    )
    security = client.web_settings if client else WebSettings.model_validate(defaults())
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=security.max_body_bytes)
    ui = ui_directory or Path(str(files("komaquill").joinpath("resources/web")))

    def selected() -> Client:
        if client is None:
            raise KomaquillError(code="invalid_configuration")
        return client

    def operation_view(value: Operation) -> OperationView:
        accepted = selected().operations.input(value.id) if value.state != "complete" else None
        return describe_operation(value, accepted)

    from komaquill.interfaces.http.library import bind_library

    bind_library(app, selected)

    @app.middleware("http")
    async def local_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        origin = request.headers.get("origin")
        cross_site = request.headers.get("sec-fetch-site") == "cross-site"
        allowed = origin is None or origin == str(request.base_url).rstrip("/")
        if not allowed or cross_site:
            return JSONResponse(
                ErrorView(
                    code="origin_not_allowed",
                    stage=OperationStage.CHECK,
                    message=message("http.origin"),
                ).model_dump(mode="json"),
                status_code=403,
            )
        peer = request.client.host if request.client else ""
        try:
            local = ip_address(peer).is_loopback
        except ValueError:
            local = peer == "testclient"
        if not local:
            if not security.origin or str(request.base_url).rstrip("/") != security.origin:
                return JSONResponse({"code": "origin_not_allowed"}, status_code=403)
            state = selected().devices.state(request.cookies.get("__Host-komaquill-device"))
            if state != "approved":
                if request.url.path == "/" and request.method == "GET" and state is None:
                    identity, token = selected().devices.enroll()
                    response = Response(
                        message("http.device_pending", identity=identity),
                        status_code=403,
                        media_type="text/plain",
                    )
                    response.set_cookie(
                        "__Host-komaquill-device",
                        token,
                        secure=True,
                        httponly=True,
                        samesite="strict",
                        max_age=security.device_lifetime_days * 86400,
                    )
                    return response
                return JSONResponse({"code": "device_not_approved"}, status_code=403)
        request.state.local_device_administrator = local
        return await call_next(request)

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
        + ([str(urlsplit(security.origin).hostname)] if security.origin else []),
        www_redirect=False,
    )

    @app.middleware("http")
    async def protect_response(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            response = await call_next(request)
        except KomaquillError as error:
            response = await failure(request, error)
        except Exception as error:
            if client is not None:
                record_event(
                    WorkspacePaths(client.workspace),
                    "http",
                    "failed",
                    failure=failure_info(error, "operation"),
                )
            response = await unexpected(request, error)
        response.headers.update(
            {
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                ),
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            }
        )
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(KomaquillError)
    async def failure(_: Request, error: KomaquillError) -> JSONResponse:
        if client is not None and error.code in {
            "storage_failed",
            "internal_error",
            "operation_failed",
        }:
            record_event(
                WorkspacePaths(client.workspace),
                "http",
                "failed",
                failure=failure_info(error, "operation"),
            )
        status = (
            404
            if error.code == "request_not_found"
            else 409
            if error.code in {"operation_busy", "operation_conflict"}
            else 400
        )
        return JSONResponse(
            ErrorView(code=error.code, stage=error.stage, message=error_message(error)).model_dump(
                mode="json"
            ),
            status_code=status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            ErrorView(
                code="invalid_request", stage=OperationStage.CHECK, message=message("http.invalid")
            ).model_dump(mode="json"),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            ErrorView(
                code="operation_failed",
                stage=OperationStage.OPERATION,
                message=message("http.failed"),
            ).model_dump(mode="json"),
            status_code=500,
        )

    @app.get("/api/v1/settings", response_model=SettingsView, operation_id="get_settings")
    def settings() -> dict[str, Any]:
        return {
            "profiles": [profile.model_dump() for profile in selected().conversations.profiles()],
            "sample_input": "Hello",
        }

    @app.get("/api/v1/access", operation_id="check_device_access")
    def device_access() -> dict[str, bool]:
        return {"approved": True}

    @app.get("/api/v1/devices", operation_id="list_devices")
    def list_devices(request: Request) -> Response:
        if not request.state.local_device_administrator:
            return JSONResponse({"code": "device_administrator_required"}, status_code=403)
        return JSONResponse(selected().devices.list())

    @app.post("/api/v1/devices/{identity}/approve", operation_id="approve_device")
    def approve_device(identity: UUID, request: Request) -> Response:
        if not request.state.local_device_administrator:
            return JSONResponse({"code": "device_administrator_required"}, status_code=403)
        selected().devices.decide(identity, approve=True)
        return Response(status_code=204)

    @app.delete("/api/v1/devices/{identity}", operation_id="revoke_device")
    def revoke_device(identity: UUID, request: Request) -> Response:
        if not request.state.local_device_administrator:
            return JSONResponse({"code": "device_administrator_required"}, status_code=403)
        selected().devices.decide(identity, approve=False)
        return Response(status_code=204)

    @app.get(
        "/api/v1/conversations",
        response_model=list[Conversation],
        operation_id="list_conversations",
    )
    def list_conversations() -> list[Conversation]:
        return selected().conversations.list()

    @app.post(
        "/api/v1/availability", response_model=Availability, operation_id="check_availability"
    )
    def availability(body: AvailabilityRequest, refresh: bool = False) -> Availability:
        return selected().availability.check(body, refresh=refresh)

    @app.post(
        "/api/v1/conversations",
        response_model=Conversation,
        status_code=201,
        operation_id="create_conversation",
    )
    def create_conversation(definition: ConversationInput) -> Conversation:
        return selected().conversations.create(definition)

    @app.get(
        "/api/v1/conversations/{identity}",
        response_model=Conversation,
        operation_id="get_conversation",
    )
    def conversation(identity: UUID) -> Conversation:
        return selected().conversations.overview(identity)

    @app.get(
        "/api/v1/conversations/{identity}/turns", response_model=TurnPage, operation_id="get_turns"
    )
    def turns(identity: UUID, before: int | None = None, limit: int = 30) -> dict[str, object]:
        return selected().conversations.turns(identity, before, limit)

    @app.put(
        "/api/v1/conversations/{identity}/notes",
        response_model=Conversation,
        operation_id="save_notes",
    )
    def notes(identity: UUID, body: NotesRequest) -> Conversation:
        return selected().conversations.save_notes(identity, body)

    @app.get(
        "/api/v1/conversations/{identity}/bookmarks",
        response_model=list[Bookmark],
        operation_id="get_bookmarks",
    )
    def bookmarks(identity: UUID) -> list[Bookmark]:
        return selected().conversations.bookmarks(identity)

    @app.post(
        "/api/v1/conversations/{identity}/bookmarks",
        response_model=Bookmark,
        operation_id="save_bookmark",
    )
    def bookmark(identity: UUID, body: SceneRequest) -> Bookmark:
        return selected().conversations.bookmark(identity, body)

    @app.post(
        "/api/v1/conversations/{identity}/fork",
        response_model=Conversation,
        operation_id="fork_conversation",
    )
    def fork(identity: UUID, body: SceneRequest) -> Conversation:
        return selected().conversations.fork(identity, body)

    @app.get("/api/v1/conversations/{identity}/export", operation_id="export_conversation")
    def export(
        identity: UUID, include_images: bool = True, include_conditions: bool = False
    ) -> FileResponse:
        path = selected().export_conversation(
            identity, include_images=include_images, include_conditions=include_conditions
        )
        return FileResponse(
            path,
            filename="conversation.zip",
            media_type="application/zip",
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/api/v1/conversations/{identity}/operations",
        response_model=list[OperationView],
        operation_id="get_conversation_operations",
    )
    def conversation_operations(identity: UUID) -> list[OperationView]:
        return [operation_view(value) for value in selected().conversations.operations(identity)]

    @app.post(
        "/api/v1/conversations/{identity}/turns",
        response_model=OperationView,
        status_code=202,
        operation_id="send_turn",
    )
    def send(identity: UUID, body: SendInput, tasks: BackgroundTasks) -> OperationView:
        service = selected().conversations
        operation, created = service.accept(identity, body)
        if created:
            tasks.add_task(execute_retained, service.execute, operation.id)
        return operation_view(operation)

    @app.get(
        "/api/v1/conversations/{identity}/actions",
        response_model=list[ActionRecord],
        operation_id="get_actions",
    )
    def get_actions(identity: UUID) -> list[ActionRecord]:
        return selected().conversations.actions(identity)

    @app.post(
        "/api/v1/conversations/{identity}/proposals/{proposal_id}/dismiss",
        response_model=Conversation,
        operation_id="dismiss_proposal",
    )
    def dismiss_proposal(
        identity: UUID, proposal_id: UUID, body: ProposalDismissal
    ) -> Conversation:
        return selected().conversations.dismiss_proposal(identity, proposal_id, body)

    @app.post(
        "/api/v1/conversations/{identity}/resume",
        response_model=Conversation,
        operation_id="resume_conversation",
    )
    def resume(identity: UUID) -> Conversation:
        return selected().conversations.resume(identity)

    @app.get(
        "/api/v1/operations/{identity}", response_model=OperationView, operation_id="get_operation"
    )
    def operation(identity: UUID) -> OperationView:
        return operation_view(selected().operations.get(identity))

    @app.post(
        "/api/v1/operations/{identity}/resume",
        response_model=OperationView,
        operation_id="resume_operation",
    )
    def resume_operation(
        identity: UUID, tasks: BackgroundTasks, allow_real_llm: bool = False
    ) -> OperationView:
        service = selected().conversations
        result = service.resume_operation(identity, allow_real_llm=allow_real_llm, execute=False)
        if result.state == "accepted":
            tasks.add_task(execute_retained, service.execute, identity)
        return operation_view(result)

    @app.get("/api/v1/images", response_model=list[Run], operation_id="list_images")
    def images() -> list[Run]:
        return selected().images.list()

    @app.post(
        "/api/v1/inputs", response_model=InputArtifact, status_code=201, operation_id="import_input"
    )
    def import_input(
        name: str,
        content: Annotated[bytes, Body(media_type="application/octet-stream")],
        kind: Literal["image", "mask"] = "image",
    ) -> InputArtifact:
        return selected().inputs.import_bytes(content, name=name, kind=kind)

    @app.post("/api/v1/images", response_model=Run, status_code=202, operation_id="submit_image")
    def submit_image(body: ImageInput) -> Run:
        return selected().images.submit(
            body.prompt,
            run_id=body.request_id,
            seed=body.seed,
            model=body.model,
            llm_profile=body.llm_profile,
            allow_real_llm=body.allow_real_llm,
        )

    @app.get("/api/v1/images/{identity}", response_model=Run, operation_id="get_image")
    def get_image(identity: UUID) -> Run:
        return selected().images.get(identity)

    @app.post("/api/v1/images/{identity}/collect", response_model=Run, operation_id="collect_image")
    def collect_image(identity: UUID) -> Run:
        selected().images.collect(identity)
        return selected().images.get(identity)

    @app.get("/api/v1/images/{identity}/files/{index}", operation_id="get_image_file")
    def image_file(identity: UUID, index: int) -> FileResponse:
        run = selected().images.get(identity)
        if index < 0 or index >= len(run.images):
            raise KomaquillError(code="request_not_found")
        return FileResponse(
            selected().artifact_path(f"runs/{identity}/{run.images[index]}"),
            media_type={
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(Path(run.images[index]).suffix.lower(), "application/octet-stream"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/runtime", response_model=SessionStatus, operation_id="get_runtime")
    def runtime() -> SessionStatus:
        return selected().runtime.status(refresh=False)

    @app.post(
        "/api/v1/runtime/refresh", response_model=SessionStatus, operation_id="refresh_runtime"
    )
    def refresh_runtime() -> SessionStatus:
        return selected().runtime.status()

    @app.get("/api/v1/runtime/views", operation_id="get_runtime_views")
    def runtime_views() -> list[dict[str, str]]:
        return selected().runtime.views()

    @app.get("/api/v1/runtime/workflows/{profile}/{view}", operation_id="get_runtime_workflow")
    def runtime_workflow(profile: str, view: Literal["app", "graph"]) -> JSONResponse:
        graph = selected().runtime.workflow(profile, view)
        return JSONResponse(
            graph, headers={"Content-Disposition": 'attachment; filename="workflow.json"'}
        )

    @app.post("/api/v1/runtime/start", response_model=SessionStatus, operation_id="start_runtime")
    def start_runtime(body: RuntimeStart) -> SessionStatus:
        if body.backend not in {"external", "colab"}:
            raise KomaquillError(code="invalid_request")
        return selected().runtime.start(backend=body.backend, endpoint=body.endpoint)

    @app.post("/api/v1/runtime/stop", response_model=SessionStatus, operation_id="stop_runtime")
    def stop_runtime() -> SessionStatus:
        return selected().runtime.stop()

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"code": "request_not_found"}, status_code=404)
        target = (ui / path).resolve()
        if target.is_relative_to(ui.resolve()) and target.is_file():
            return FileResponse(
                target,
                headers={"Cache-Control": "public, max-age=31536000, immutable"}
                if path.startswith("assets/")
                else {"Cache-Control": "no-cache"},
            )
        if (ui / "index.html").is_file() and "." not in Path(path).name:
            return FileResponse(ui / "index.html", headers={"Cache-Control": "no-cache"})
        return JSONResponse({"code": "request_not_found"}, status_code=404)

    return app


def export_schema(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(create_app().openapi(), indent=2) + "\n", encoding="utf-8")
