"""Persist generation requests and collection results atomically."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from filelock import FileLock, Timeout

from komaquill.adapters.atomic import atomic_json, read_bytes
from komaquill.adapters.paths import WorkspacePaths, artifact_path
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject, Run, Session
from komaquill.domain.workflow import digest


def run_dir(project: WorkspacePaths, run_id: UUID) -> Path:
    """Use a local UUID rather than a remote filename for directories."""
    return artifact_path(project.data, f"runs/{run_id}")


def save_run(project: WorkspacePaths, run: Run) -> None:
    """Commit new image metadata without rewriting archived files."""
    from komaquill.adapters.image_repository import save

    save(project, run)


def load_runs(project: WorkspacePaths, *, unreadable: list[UUID | str] | None = None) -> list[Run]:
    """Load current image requests from the shared database."""
    from komaquill.adapters.image_repository import load

    return load(project, unreadable)


def load_session(project: WorkspacePaths) -> Session:
    """Read the saved connection."""
    return Session.model_validate_json(read_bytes(project.session_file))


def stop_request(project: WorkspacePaths, session: Session) -> bool | None:
    """Read a stop request for the current runtime only."""
    path = project.state / "stop.json"
    if not path.is_file():
        return None
    request = json.loads(read_bytes(path))
    if request.get("runtime_id") != str(session.runtime_id):
        return None
    return bool(request.get("force"))


def save_conditions(project: WorkspacePaths, run: Run, prompt: list[Any]) -> None:
    """Record observed conditions before generation completes."""
    folder = run_dir(project, run.run_id)
    graph = prompt[2]
    if not isinstance(graph, dict):
        raise KomaquillError(display_key="text.generation_conditions_are_invalid")
    observed = folder / "workflow-api.json"
    if not observed.is_file():
        atomic_json(observed, graph)
    try:
        recorded = json.loads(observed.read_bytes())
    except (OSError, ValueError) as error:
        raise KomaquillError(code="storage_failed", stage="storage", retryable=False) from error
    if (
        not isinstance(recorded, dict)
        or recorded.keys() != graph.keys()
        or any(
            not isinstance(recorded[key], dict)
            or not isinstance(graph[key], dict)
            or any(
                recorded[key].get(field) != graph[key].get(field)
                for field in ("class_type", "inputs")
            )
            for key in graph
        )
    ):
        raise KomaquillError(code="invalid_output", stage="history_validation", retryable=False)
    graph_digest = digest(recorded)
    if run.workflow_sha256 != graph_digest:
        run.workflow_sha256 = graph_digest
        save_run(project, run)
    extra = prompt[3] if len(prompt) > 3 else {}
    if isinstance(extra, dict):
        extension = extra.get("komaquill", {})
        supplied = extension.get("input_sha256", {}) if isinstance(extension, dict) else {}
        if isinstance(supplied, dict):
            import re

            observed_inputs = {
                name: sha
                for name, sha in supplied.items()
                if isinstance(name, str)
                and isinstance(sha, str)
                and re.fullmatch(r"[0-9a-f]{64}", sha)
            }
            if any(
                name in run.input_sha256 and run.input_sha256[name] != sha
                for name, sha in observed_inputs.items()
            ):
                raise KomaquillError(
                    code="invalid_output", stage="history_validation", retryable=False
                )
            run.input_sha256.update(observed_inputs)
        metadata = extra.get("extra_pnginfo", {})
        ui = metadata.get("workflow") if isinstance(metadata, dict) else None
        if isinstance(ui, dict) and not (folder / "workflow.json").is_file():
            atomic_json(folder / "workflow.json", ui)


class ImageRepository:
    """Commit individual files atomically and finalize saved state last."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.project.state.mkdir(parents=True, exist_ok=True)
        with (
            FileLock(str(self.project.state / ".collection.lock")),
        ):
            yield

    def load(self, unreadable: list[UUID | str] | None = None) -> list[Run]:
        return load_runs(self.project, unreadable=unreadable)

    def read(self, run_id: UUID) -> Run:
        from komaquill.adapters.image_repository import read

        return read(self.project, run_id)

    def save(self, run: Run) -> None:
        save_run(self.project, run)

    def conditions(self, run: Run, prompt: list[Any]) -> None:
        save_conditions(self.project, run, prompt)

    def prepare(self, run: Run, graph: JsonObject, ui: JsonObject) -> None:
        folder = run_dir(self.project, run.run_id)
        atomic_json(folder / "submitted-workflow-api.json", graph)
        atomic_json(folder / "submitted-workflow.json", ui)

    @contextmanager
    def claim(self, run_id: UUID) -> Iterator[bool]:
        """Serialize one request without blocking unrelated state updates."""
        folder = self.project.state / "collection"
        folder.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(folder / f"{run_id}.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            yield False
            return
        try:
            yield True
        finally:
            lock.release()
