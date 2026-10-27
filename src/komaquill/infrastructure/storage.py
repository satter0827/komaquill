"""Persist generation requests and collection results atomically."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from filelock import FileLock, Timeout

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject, Run, Session
from komaquill.domain.workflow import digest
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.locations import Locations
from komaquill.infrastructure.paths import WorkspacePaths


def run_dir(project: WorkspacePaths, run_id: UUID) -> Path:
    """Use a local UUID rather than a remote filename for directories."""
    locations = Locations(project)
    try:
        return locations.run(run_id)
    except FileNotFoundError:
        return locations.register_run(run_id)


def save_run(project: WorkspacePaths, run: Run) -> None:
    """Persist changed request state and emit state transitions."""
    path = run_dir(project, run.run_id) / "run.json"
    previous = Run.model_validate_json(path.read_bytes()) if path.is_file() else None
    if previous is not None and previous.run_id != run.run_id:
        raise KomaquillError(code="identity_mismatch", stage="storage")
    if previous == run:
        return
    run.updated_at = datetime.now(UTC)
    if run.state == "saved" and run.saved_at is None:
        run.saved_at = run.updated_at
    atomic_json(path, run.model_dump(mode="json"))
    if run.first_candidate_available_at and (
        previous is None or previous.first_candidate_available_at is None
    ):
        record_event(
            project,
            "image_availability",
            "available",
            runtime_id=run.runtime_id,
            run_id=run.run_id,
        )
    Locations(project).recorded(run.run_id)
    if previous is None or previous.state != run.state or previous.failure != run.failure:
        record_event(
            project,
            "generation",
            run.state,
            runtime_id=run.runtime_id,
            run_id=run.run_id,
            failure=run.failure,
        )
    if (
        run.collection_failure is not None or (previous and previous.collection_failure is not None)
    ) and (previous is None or previous.collection_failure != run.collection_failure):
        record_event(
            project,
            "image_collection",
            "failed" if run.collection_failure else "recovered",
            runtime_id=run.runtime_id,
            run_id=run.run_id,
            failure=run.collection_failure,
        )


def load_runs(project: WorkspacePaths, *, unreadable: list[UUID | str] | None = None) -> list[Run]:
    """Read histories, optionally reporting unreadable identities while continuing."""
    runs: list[Run] = []
    locations = Locations(project)
    placements = locations.read().runs
    candidates = [
        (identity, locations.path(row.folder) / "run.json", row.recorded)
        for identity, row in placements.items()
    ]
    known_paths = {path for _, path, _ in candidates}
    orphan_ids: set[UUID | str] = set()
    paths = list(project.runs.glob("*/run.json"))
    paths.extend((project.data / "stories").glob("*/renders/*/runs/*/run.json"))
    for path in paths:
        if path in known_paths:
            continue
        try:
            identity: UUID | str = UUID(path.parent.name)
        except ValueError:
            identity = path.parent.relative_to(project.data).as_posix()
        if unreadable is None:
            raise KomaquillError(
                code="storage_failed",
                stage="history_validation",
                details={"identity": str(identity)},
            )
        if identity not in unreadable:
            unreadable.append(identity)
        orphan_ids.add(identity)
    for identity, path, recorded in candidates:
        if identity in orphan_ids:
            continue
        if not path.exists() and not recorded:
            continue
        try:
            run = Run.model_validate_json(path.read_bytes())
            if run.run_id != identity:
                raise ValueError("The run ID differs from its placement record.")
        except (OSError, ValueError):
            if unreadable is None:
                raise KomaquillError(
                    code="storage_failed",
                    stage="history_validation",
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


class FileRunRepository:
    """Commit individual files atomically and finalize saved state last."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.project.state.mkdir(parents=True, exist_ok=True)
        with (
            FileLock(str(self.project.state / ".collection.lock")),
            Locations(self.project).snapshot(fresh=True),
        ):
            yield

    def load(self, unreadable: list[UUID | str] | None = None) -> list[Run]:
        return load_runs(self.project, unreadable=unreadable)

    def read(self, run_id: UUID) -> Run:
        locations = Locations(self.project)
        try:
            run = Run.model_validate_json((locations.run(run_id) / "run.json").read_bytes())
        except FileNotFoundError:
            placement = locations.read().runs.get(run_id)
            if placement is not None and placement.recorded:
                raise KomaquillError(
                    code="storage_failed",
                    stage="storage",
                    retryable=False,
                    details={"run_id": str(run_id)},
                ) from None
            raise
        except ValueError:
            raise KomaquillError(
                code="storage_failed",
                stage="storage",
                retryable=False,
                details={"run_id": str(run_id)},
            ) from None
        if run.run_id != run_id:
            raise KomaquillError(code="identity_mismatch", stage="storage")
        return run

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
