"""Persist generation requests and collection results atomically."""

import hashlib
import io
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from filelock import FileLock
from PIL import Image, UnidentifiedImageError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import ImageMetadata, ImageRef, JsonObject, Run, Session
from komaquill.domain.workflow import digest
from komaquill.infrastructure.diagnostics import record_event
from komaquill.messages import message

if TYPE_CHECKING:
    from komaquill.application.ports import GenerationBackend
from komaquill.infrastructure.paths import WorkspacePaths

WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})
REPLACE_TIMEOUT_SECONDS = 1.0
REPLACE_RETRY_SECONDS = 0.05


def atomic_bytes(path: Path, content: bytes) -> None:
    """Replace a file using a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        deadline = None
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError as error:
                if (
                    sys.platform != "win32"
                    or getattr(error, "winerror", None) not in WINDOWS_REPLACE_ERRORS
                ):
                    raise
                now = time.monotonic()
                if deadline is None:
                    deadline = now + REPLACE_TIMEOUT_SECONDS
                elif now >= deadline:
                    raise
                time.sleep(REPLACE_RETRY_SECONDS)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, data: JsonObject) -> None:
    """Write UTF-8 JSON atomically."""
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def run_dir(project: WorkspacePaths, run_id: UUID) -> Path:
    """Use a local UUID rather than a remote filename for directories."""
    return project.runs / str(run_id)


def save_run(project: WorkspacePaths, run: Run) -> None:
    """Persist changed request state and emit state transitions."""
    path = run_dir(project, run.run_id) / "run.json"
    previous = Run.model_validate_json(path.read_bytes()) if path.is_file() else None
    if previous == run:
        return
    run.updated_at = datetime.now(UTC)
    if run.state == "saved" and run.saved_at is None:
        run.saved_at = run.updated_at
    atomic_json(path, run.model_dump(mode="json"))
    if previous is None or previous.state != run.state or previous.failure != run.failure:
        record_event(
            project,
            "generation",
            run.state,
            runtime_id=run.runtime_id,
            run_id=run.run_id,
            failure=run.failure,
        )


def load_runs(project: WorkspacePaths, *, unreadable: list[UUID | str] | None = None) -> list[Run]:
    """Read histories, optionally reporting unreadable identities while continuing."""
    runs: list[Run] = []
    for path in project.runs.glob("*/run.json"):
        try:
            identity = UUID(path.parent.name)
        except ValueError:
            if unreadable is None:
                raise KomaquillError(display_key="text.the_run_directory_name_is_invalid") from None
            unreadable.append(f"runs/{path.parent.name!r}")
            continue
        try:
            run = Run.model_validate_json(path.read_bytes())
            if run.run_id != identity:
                raise ValueError(message("text.the_run_id_differs_from_its_directory_name"))
        except (OSError, ValueError):
            if unreadable is None:
                raise KomaquillError(
                    display_key="text.cannot_read_run_history_check_the_file_and",
                    details={"identity": str(identity)},
                ) from None
            unreadable.append(identity)
            continue
        runs.append(run)
    return runs


def load_session(project: WorkspacePaths) -> Session:
    """Read the saved connection."""
    return Session.model_validate_json(project.session_file.read_bytes())


def stop_request(project: WorkspacePaths, session: Session) -> bool | None:
    """Read a stop request for the current runtime only."""
    path = project.state / "stop.json"
    if not path.is_file():
        return None
    request = json.loads(path.read_text(encoding="utf-8"))
    if request.get("runtime_id") != str(session.runtime_id):
        return None
    return bool(request.get("force"))


def save_images(
    project: WorkspacePaths, client: "GenerationBackend", run: Run, record: JsonObject
) -> list[str]:
    """Verify each image and persist its integrity information before continuing."""
    image_names: list[str] = []
    outputs = record.get("outputs", {})
    if not isinstance(outputs, dict):
        raise KomaquillError(
            display_key="text.the_image_output_structure_is_invalid", code="invalid_output"
        )
    for output in outputs.values():
        if not isinstance(output, dict) or not isinstance(output.get("images", []), list):
            raise KomaquillError(
                display_key="text.the_image_output_structure_is_invalid", code="invalid_output"
            )
        for raw_reference in output.get("images", []):
            reference = ImageRef.model_validate(raw_reference)
            suffix = PurePosixPath(reference.filename).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
                raise KomaquillError(
                    display_key="text.the_image_format_is_unsupported", code="unsupported_image"
                )
            name = f"images/{len(image_names) + 1:04d}{suffix}"
            target = run_dir(project, run.run_id) / name
            previous = run.image_metadata.get(name)
            content = target.read_bytes() if target.is_file() else None
            if (
                content is None
                or previous is None
                or (hashlib.sha256(content).hexdigest() != previous.sha256)
            ):
                content = client.image(reference)
            try:
                with Image.open(io.BytesIO(content)) as image:
                    width, height = image.size
                    image.verify()
            except (UnidentifiedImageError, OSError, SyntaxError) as error:
                raise KomaquillError(
                    display_key="text.image_verification_failed_run_collect_to_retry",
                    code="image_invalid",
                    stage="image_validation",
                ) from error
            if not target.is_file() or target.read_bytes() != content:
                atomic_bytes(target, content)
            run.image_metadata[name] = ImageMetadata(
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                width=width,
                height=height,
                source=reference,
            )
            image_names.append(name)
            run.images = list(image_names)
            save_run(project, run)
    if not image_names:
        raise KomaquillError(
            display_key="text.no_output_images_were_found_check_the_workflow", code="missing_image"
        )
    return image_names


def save_conditions(project: WorkspacePaths, run: Run, prompt: list[Any]) -> None:
    """Record observed conditions before generation completes."""
    folder = run_dir(project, run.run_id)
    graph = prompt[2]
    if not isinstance(graph, dict):
        raise KomaquillError(display_key="text.generation_conditions_are_invalid")
    if not (folder / "workflow-api.json").is_file():
        atomic_json(folder / "workflow-api.json", graph)
    graph_digest = digest(graph)
    if run.workflow_sha256 != graph_digest:
        run.workflow_sha256 = graph_digest
        save_run(project, run)
    extra = prompt[3] if len(prompt) > 3 else {}
    if isinstance(extra, dict):
        extension = extra.get("komaquill", {})
        supplied = extension.get("input_sha256", {}) if isinstance(extension, dict) else {}
        if isinstance(supplied, dict):
            import re

            run.input_sha256 = {
                name: sha
                for name, sha in supplied.items()
                if isinstance(name, str)
                and isinstance(sha, str)
                and re.fullmatch(r"[0-9a-f]{64}", sha)
            }
        metadata = extra.get("extra_pnginfo", {})
        ui = metadata.get("workflow") if isinstance(metadata, dict) else None
        if isinstance(ui, dict) and not (folder / "workflow.json").is_file():
            atomic_json(folder / "workflow.json", ui)


class FileRunRepository:
    """Commit individual files atomically and finalize saved state last."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project

    def lock(self) -> FileLock:
        self.project.runs.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.project.runs / ".collection.lock"))

    def load(self, unreadable: list[UUID | str] | None = None) -> list[Run]:
        return load_runs(self.project, unreadable=unreadable)

    def read(self, run_id: UUID) -> Run:
        return Run.model_validate_json((run_dir(self.project, run_id) / "run.json").read_bytes())

    def save(self, run: Run) -> None:
        save_run(self.project, run)

    def conditions(self, run: Run, prompt: list[Any]) -> None:
        save_conditions(self.project, run, prompt)

    def prepare(self, run: Run, graph: JsonObject, ui: JsonObject) -> None:
        folder = run_dir(self.project, run.run_id)
        atomic_json(folder / "workflow-api.json", graph)
        atomic_json(folder / "workflow.json", ui)

    def images(self, backend: "GenerationBackend", run: Run, record: JsonObject) -> list[str]:
        return save_images(self.project, backend, run, record)
