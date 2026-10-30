"""Expose local-only HTTP routes over the same public client used by the CLI."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import BackgroundTasks, Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from komaquill.api import Client, KomaquillError, Run, SessionStatus
from komaquill.conversations.models import (
    Conversation,
    ConversationInput,
    Operation,
    SendInput,
    StateCommand,
)
from komaquill.domain.materials import InputArtifact
from komaquill.messages import error_message, message

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    prompt: str = Field(min_length=1, max_length=20000)
    seed: int | None = Field(default=None, ge=0)
    model: str | None = None


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
    error_message: str | None = None
    image_error_message: str | None = None


def describe_operation(value: Operation) -> OperationView:
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
        error_message=describe(value.error),
        image_error_message=describe(value.image_error),
    )


def create_app(client: Client | None = None, *, ui_directory: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if client is not None:
            client.configure()
            client.operations.reconcile()
        yield

    app = FastAPI(title="Komaquill API", version="1.0.0", lifespan=lifespan)
    ui = ui_directory or Path(str(files("komaquill").joinpath("resources/web")))

    def selected() -> Client:
        if client is None:
            raise KomaquillError(code="invalid_configuration")
        return client

    @app.middleware("http")
    async def local_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.url.hostname
        origin = request.headers.get("origin")
        cross_site = request.headers.get("sec-fetch-site") == "cross-site"
        allowed = origin is None or origin == str(request.base_url).rstrip("/")
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"} or not allowed or cross_site:
            return JSONResponse(
                {
                    "code": "origin_not_allowed",
                    "message": message("http.origin"),
                },
                status_code=403,
            )
        try:
            return await call_next(request)
        except Exception:
            return JSONResponse(
                {"code": "operation_failed", "message": message("http.failed")}, status_code=500
            )

    @app.exception_handler(KomaquillError)
    async def failure(_: Request, error: KomaquillError) -> JSONResponse:
        status = (
            404
            if error.code == "request_not_found"
            else 409
            if error.code in {"operation_busy", "operation_conflict"}
            else 400
        )
        return JSONResponse(
            {"code": error.code, "message": error_message(error)}, status_code=status
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"code": "invalid_request", "message": message("http.invalid")},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {
                "code": "operation_failed",
                "message": message("http.failed"),
            },
            status_code=500,
        )

    @app.get("/api/v1/settings", response_model=SettingsView, operation_id="get_settings")
    def settings() -> dict[str, Any]:
        return {
            "profiles": [profile.model_dump() for profile in selected().conversations.profiles()],
            "sample_input": "Hello",
        }

    @app.get(
        "/api/v1/conversations",
        response_model=list[Conversation],
        operation_id="list_conversations",
    )
    def list_conversations() -> list[Conversation]:
        return selected().conversations.list()

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
        return selected().conversations.get(identity)

    @app.get(
        "/api/v1/conversations/{identity}/operations",
        response_model=list[OperationView],
        operation_id="get_conversation_operations",
    )
    def conversation_operations(identity: UUID) -> list[OperationView]:
        return [
            describe_operation(value) for value in selected().conversations.operations(identity)
        ]

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
            tasks.add_task(service.execute, operation.id)
        return describe_operation(operation)

    @app.post(
        "/api/v1/conversations/{identity}/commands",
        response_model=Conversation,
        operation_id="apply_command",
    )
    def command(identity: UUID, body: StateCommand) -> Conversation:
        return selected().conversations.command(identity, body)

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
        return describe_operation(selected().operations.get(identity))

    @app.get("/api/v1/images", response_model=list[Run], operation_id="list_images")
    def images() -> list[Run]:
        return selected().images.list()

    @app.post(
        "/api/v1/inputs", response_model=InputArtifact, status_code=201, operation_id="import_input"
    )
    def import_input(
        name: str,
        content: Annotated[
            bytes, Body(media_type="application/octet-stream", max_length=MAX_UPLOAD_BYTES)
        ],
        kind: Literal["image", "mask"] = "image",
    ) -> InputArtifact:
        return selected().inputs.import_bytes(content, name=name, kind=kind)

    @app.post("/api/v1/images", response_model=Run, status_code=202, operation_id="submit_image")
    def submit_image(body: ImageInput) -> Run:
        return selected().images.submit(
            body.prompt, run_id=body.request_id, seed=body.seed, model=body.model
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
        return FileResponse(selected().artifact_path(f"runs/{identity}/{run.images[index]}"))

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
