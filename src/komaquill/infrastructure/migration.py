"""Move story-owned files with resumable copy, verification and explicit source cleanup."""

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid4

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

from komaquill.domain.errors import KomaquillError
from komaquill.domain.workflow import digest
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.locations import Locations, RunPlacement, StorageIndex, story_folder
from komaquill.infrastructure.monitor_state import alive, read_monitor
from komaquill.infrastructure.sessions.connection import monitor_lock


class MigrationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    temporary: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    copied: bool = False
    removed: bool = False


class MigrationState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    created_at: str
    producer: dict[str, Any]
    finished_at: str | None = None
    status: Literal["copying", "removing_sources", "complete"]
    moves: list[MigrationStep]
    index: StorageIndex


def checksum(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def stopped(project: Project) -> None:
    """Reject migration without taking ownership of any provider resource."""
    if alive(read_monitor(project)) or (project.state / "colab.json").exists():
        raise KomaquillError(code="invalid_request", stage="storage", retryable=False)
    paths = list(project.runs.glob("*/run.json")) + list(
        (project.data / "stories").glob("*/renders/*/runs/*/run.json")
    )
    for path in paths:
        if json.loads(path.read_bytes())["state"] not in {"saved", "failed", "lost"}:
            raise KomaquillError(code="invalid_request", stage="storage", retryable=False)


def migration_plan(project: Project) -> dict[str, Any]:
    """Resolve all destinations and capture content hashes before copying anything."""
    locations = Locations(project)
    index = StorageIndex()
    moves: list[dict[str, Any]] = []
    owned_runs: set[UUID] = set()

    def add(source: Path, target: Path) -> None:
        if source.resolve() == target.resolve():
            return
        if source.is_symlink() or not source.resolve().is_relative_to(project.data.resolve()):
            raise KomaquillError(code="invalid_request", stage="storage")
        moves.append(
            {
                "source": source.relative_to(project.data).as_posix(),
                "target": target.relative_to(project.data).as_posix(),
                "temporary": target.relative_to(project.data).as_posix()
                + f".{uuid4().hex}.migration.tmp",
                "sha256": checksum(source),
                "size_bytes": source.stat().st_size,
                "copied": False,
                "removed": False,
            }
        )

    for request_file in sorted((project.data / "stories").glob("*/request.json")):
        request = json.loads(request_file.read_bytes())
        identity = UUID(request["story_id"])
        source = request_file.parent
        if identity in index.stories:
            raise KomaquillError(code="identity_mismatch", stage="storage")
        if source.name == str(identity):
            created = datetime.fromisoformat(request["created_at"])
            destination = source.parent / story_folder(request["source_name"], created, identity)
        else:
            destination = source
        if destination != source and destination.exists():
            raise KomaquillError(code="identity_mismatch", stage="storage")
        index.stories[identity] = destination.relative_to(project.data).as_posix()
        for path in sorted(source.rglob("*")):
            if path.is_file():
                add(path, destination / path.relative_to(source))
        for render_file in sorted(source.glob("renders/*/render.json")):
            render = json.loads(render_file.read_bytes())
            plan = json.loads((source / "plan.json").read_bytes())
            if (
                digest(plan["plan"]) != plan["sha256"]
                or render["plan_sha256"] != plan["sha256"]
                or render["story_id"] != str(identity)
                or render["render_id"] != render_file.parent.name
            ):
                raise KomaquillError(code="identity_mismatch", stage="storage")
            relative = f"{index.stories[identity]}/renders/{render['render_id']}"
            for position, scene in enumerate(render["scenes"], 1):
                for attempt, run_id in enumerate(scene["run_ids"], 1):
                    run = UUID(run_id)
                    if run in owned_runs:
                        raise KomaquillError(code="identity_mismatch", stage="storage")
                    owned_runs.add(run)
                    folder = f"{relative}/runs/{run}"
                    existing = [
                        path
                        for path in (
                            project.runs / str(run),
                            render_file.parent / "runs" / str(run),
                        )
                        if (path / "run.json").exists()
                    ]
                    if len(existing) > 1:
                        raise KomaquillError(code="identity_mismatch", stage="storage")
                    recorded = bool(existing)
                    index.runs[run] = RunPlacement(
                        folder=folder,
                        images=f"{relative}/images",
                        scene=position,
                        attempt=attempt,
                        recorded=recorded,
                    )
                    if not existing or existing[0] != project.runs / str(run):
                        continue
                    source_run = existing[0]
                    metadata = json.loads((source_run / "run.json").read_bytes())
                    if metadata["run_id"] != str(run):
                        raise KomaquillError(code="identity_mismatch", stage="storage")
                    for path in sorted(source_run.rglob("*")):
                        if not path.is_file():
                            continue
                        tail = path.relative_to(source_run).as_posix()
                        target = locations.path(f"{folder}/{tail}")
                        if tail.startswith("images/"):
                            if (
                                tail not in metadata["images"]
                                or not PurePosixPath(tail).stem.isdecimal()
                            ):
                                raise KomaquillError(code="invalid_request", stage="storage")
                            target = locations.path(
                                f"{relative}/images/{position:03d}-{attempt:03d}-{int(path.stem):03d}{path.suffix}"
                            )
                        add(path, target)
    for path in sorted(project.runs.glob("*/run.json")):
        value = json.loads(path.read_bytes())
        identity = UUID(value["run_id"])
        if str(identity) != path.parent.name:
            raise KomaquillError(code="identity_mismatch", stage="storage")
        if identity not in owned_runs:
            index.runs[identity] = RunPlacement(
                folder=path.parent.relative_to(project.data).as_posix(), recorded=True
            )
    return {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "producer": project.identity(),
        "status": "copying",
        "moves": moves,
        "index": index.model_dump(mode="json"),
    }


def migrate(project: Project, *, dry_run: bool = False) -> dict[str, Any]:
    """Verify every copy before committing locations or deleting original files."""
    project.state.mkdir(parents=True, exist_ok=True)
    locations = Locations(project)
    with (
        FileLock(str(project.state / ".supervisor-start.lock")),
        monitor_lock(project).acquire(timeout=0),
        FileLock(str(project.state / ".collection.lock")),
    ):
        stopped(project)
        checkpoint = locations.migration_file
        existing = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else None
        state = (
            existing
            if existing and existing.get("status") != "complete"
            else migration_plan(project)
        )
        state = MigrationState.model_validate(state).model_dump(mode="json", exclude_none=True)
        for row in state["moves"]:
            source_parts = PurePosixPath(row["source"]).parts
            target_parts = PurePosixPath(row["target"]).parts
            if (
                len(source_parts) < 3
                or source_parts[0] not in {"stories", "runs"}
                or len(target_parts) < 3
                or target_parts[0] != "stories"
            ):
                raise KomaquillError(code="invalid_request", stage="storage")
            locations.path(row["temporary"])
        if dry_run:
            return {
                "files": len(state["moves"]),
                "logical_bytes": sum(row["size_bytes"] for row in state["moves"]),
                "destinations": [row["target"] for row in state["moves"]],
            }
        atomic_json(checkpoint, state)
        for row in state["moves"]:
            source, target = locations.path(row["source"]), locations.path(row["target"])
            if target.exists():
                if checksum(target) != row["sha256"]:
                    raise KomaquillError(code="identity_mismatch", stage="storage")
            else:
                if not source.is_file() or checksum(source) != row["sha256"]:
                    raise KomaquillError(code="storage_failed", stage="storage")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = locations.path(row["temporary"])
                if temporary.is_symlink():
                    raise KomaquillError(code="invalid_request", stage="storage")
                shutil.copy2(source, temporary)
                if checksum(temporary) != row["sha256"]:
                    raise KomaquillError(code="storage_failed", stage="storage")
                temporary.replace(target)
            row["copied"] = True
            atomic_json(checkpoint, state)
        locations.save(StorageIndex.model_validate(state["index"]))
        state["status"] = "removing_sources"
        atomic_json(checkpoint, state)
        for row in state["moves"]:
            source, target = locations.path(row["source"]), locations.path(row["target"])
            if not target.is_file() or checksum(target) != row["sha256"]:
                raise KomaquillError(code="storage_failed", stage="storage")
            if source.exists():
                if checksum(source) != row["sha256"]:
                    raise KomaquillError(code="identity_mismatch", stage="storage")
                source.unlink()
            row["removed"] = True
            atomic_json(checkpoint, state)
        roots = {
            locations.path("/".join(PurePosixPath(row["source"]).parts[:2]))
            for row in state["moves"]
        }
        parents = {
            directory
            for root in roots
            for directory in (root, *root.rglob("*"))
            if directory.is_dir()
            and not directory.is_symlink()
            and directory.resolve().is_relative_to(root.resolve())
        }
        for directory in sorted(parents, key=lambda path: len(path.parts), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        rebuilt = locations.rebuild()
        if rebuilt != StorageIndex.model_validate(state["index"]):
            raise KomaquillError(code="identity_mismatch", stage="storage")
        locations.save(rebuilt)
        state["status"] = "complete"
        state["finished_at"] = datetime.now(UTC).isoformat()
        atomic_json(checkpoint, state)
        report = (
            project.reports
            / "migrations"
            / f"story-storage-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex}.json"
        )
        atomic_json(report, state)
    from komaquill.infrastructure.stories.storage import FileStoryRepository

    repository = FileStoryRepository(project)
    for identity in rebuilt.stories:
        folder = repository.folder(identity)
        request = json.loads((folder / "request.json").read_bytes())
        from komaquill.infrastructure.atomic import atomic_bytes

        if not (folder / "source.txt").exists():
            atomic_bytes(folder / "source.txt", request["source_text"].encode("utf-8"))
        if (folder / "plan.json").exists():
            repository.publish(identity)
            for render in repository.renders(identity):
                repository.publish(identity, render.render_id)
    from komaquill.infrastructure.evaluations import image_evidence

    observations = []
    for run in project.repository.load():
        for name in run.images:
            if name not in run.image_metadata:
                observations.append(
                    {
                        "run_id": str(run.run_id),
                        **image_evidence(project, f"runs/{run.run_id}/{name}", None),
                    }
                )
    if observations:
        atomic_json(
            report.with_name(report.stem + "-observations.json"),
            {"format_version": 1, "images": observations},
        )
    return {
        "files": len(state["moves"]),
        "logical_bytes": sum(row["size_bytes"] for row in state["moves"]),
        "status": "complete",
    }
