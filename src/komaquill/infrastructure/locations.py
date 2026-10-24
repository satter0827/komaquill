"""Resolve logical storage keys using rebuildable local placement records."""

import json
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import UUID

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.paths import WorkspacePaths

SOURCE_STEM_LENGTH = 48


class RunPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    folder: str
    images: str | None = None
    scene: int | None = Field(default=None, ge=1)
    attempt: int | None = Field(default=None, ge=1)
    recorded: bool = False

    @model_validator(mode="after")
    def image_context(self) -> "RunPlacement":
        if self.images is not None and (self.scene is None or self.attempt is None):
            raise ValueError("Story images require scene and attempt positions.")
        return self


class StorageIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    stories: dict[UUID, str] = Field(default_factory=dict)
    runs: dict[UUID, RunPlacement] = Field(default_factory=dict)


_SNAPSHOTS: ContextVar[dict[Path, StorageIndex] | None] = ContextVar(
    "storage_snapshots", default=None
)


def story_folder(source_name: str, created_at: datetime, identity: UUID) -> str:
    """Keep the stored name stable and safe on both supported platforms."""
    name = Path(PureWindowsPath(source_name).name).stem
    stem = re.sub(r"[^\w.-]+", "-", unicodedata.normalize("NFKC", name)).strip(" .-_")
    stem = stem[:SOURCE_STEM_LENGTH] or "story"
    return f"{created_at:%Y%m%d}-{stem}-{identity.hex[:8]}"


class Locations:
    """Keep placement knowledge out of generation, evaluation and public contracts."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project
        self.root = project.data
        self.index_file = self.root / "locations.json"
        self.migration_file = project.state / "story-storage-migration.json"

    def path(self, key: str) -> Path:
        relative = PurePosixPath(key)
        target = self.root.joinpath(*relative.parts)
        if (
            not key
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in key
            or ":" in key
            or not target.resolve().is_relative_to(self.root.resolve())
        ):
            raise KomaquillError(code="invalid_request", stage="storage")
        return target

    def legacy_exists(self) -> bool:
        for path in (self.root / "runs").glob("*/run.json"):
            try:
                UUID(path.parent.name)
            except ValueError:
                continue
            return True
        return next((self.root / "stories").glob("*/request.json"), None) is not None

    def read(self, *, migrating: bool = False) -> StorageIndex:
        snapshots = _SNAPSHOTS.get()
        if not migrating and snapshots is not None and self.root in snapshots:
            return snapshots[self.root].model_copy(deep=True)
        if not migrating and self.migration_file.is_file():
            state = json.loads(self.migration_file.read_bytes())
            if state.get("status") != "complete":
                raise KomaquillError(code="migration_required", stage="storage")
        if self.index_file.is_file():
            value = StorageIndex.model_validate_json(self.index_file.read_bytes())
            for folder in value.stories.values():
                self.path(folder)
            for row in value.runs.values():
                self.path(row.folder)
                if row.images:
                    self.path(row.images)
            if not migrating and snapshots is not None:
                snapshots[self.root] = value.model_copy(deep=True)
            return value
        if not migrating and self.legacy_exists():
            raise KomaquillError(code="migration_required", stage="storage")
        return StorageIndex()

    @contextmanager
    def snapshot(self, *, fresh: bool = False) -> Iterator[None]:
        """Share one validated index within a bounded read operation."""
        previous = _SNAPSHOTS.get()
        if previous is not None and self.root in previous and not fresh:
            yield
            return
        snapshots = dict(previous or {})
        snapshots.pop(self.root, None)
        token = _SNAPSHOTS.set(snapshots)
        try:
            self.read()
            yield
        finally:
            _SNAPSHOTS.reset(token)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Invalidate read snapshots after acquiring the mutation lock."""
        self.project.state.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.project.state / ".locations.lock")):
            snapshots = _SNAPSHOTS.get()
            if snapshots is not None:
                snapshots.pop(self.root, None)
            try:
                yield
            finally:
                if snapshots is not None:
                    snapshots.pop(self.root, None)

    def save(self, index: StorageIndex) -> None:
        atomic_json(self.index_file, index.model_dump(mode="json"))

    def ensure(self) -> None:
        with self.lock():
            value = self.read()
            if not self.index_file.exists():
                self.save(value)

    def register_story(self, identity: UUID, source_name: str, created_at: datetime) -> Path:
        with self.lock():
            index = self.read()
            if identity in index.stories:
                raise FileExistsError(str(identity))
            folder = f"stories/{story_folder(source_name, created_at, identity)}"
            if self.path(folder).exists() or folder in index.stories.values():
                raise KomaquillError(code="invalid_request", stage="storage")
            index.stories[identity] = folder
            self.save(index)
            return self.path(folder)

    def story(self, identity: UUID) -> Path:
        folder = self.read().stories.get(identity)
        if folder is None:
            raise FileNotFoundError(str(identity))
        return self.path(folder)

    def register_run(self, identity: UUID, placement: RunPlacement | None = None) -> Path:
        with self.lock():
            index = self.read()
            expected = placement or RunPlacement(folder=f"runs/{identity}")
            previous = index.runs.get(identity)
            if previous is not None:
                if placement is not None and previous.model_copy(
                    update={"recorded": False}
                ) != expected.model_copy(update={"recorded": False}):
                    raise KomaquillError(code="identity_mismatch", stage="storage")
                return self.path(previous.folder)
            self.path(expected.folder)
            if expected.images is not None:
                self.path(expected.images)
            if any(row.folder == expected.folder for row in index.runs.values()):
                raise KomaquillError(code="identity_mismatch", stage="storage")
            index.runs[identity] = expected
            self.save(index)
            return self.path(expected.folder)

    def run(self, identity: UUID) -> Path:
        row = self.read().runs.get(identity)
        if row is None:
            raise FileNotFoundError(str(identity))
        return self.path(row.folder)

    def recorded(self, identity: UUID) -> None:
        if self.read().runs[identity].recorded:
            return
        with self.lock():
            index = self.read()
            row = index.runs[identity]
            if not row.recorded:
                index.runs[identity] = row.model_copy(update={"recorded": True})
                self.save(index)

    def artifact(self, key: str) -> Path:
        self.path(key)
        parts = PurePosixPath(key).parts
        if len(parts) < 3 or parts[0] != "runs":
            return self.path(key)
        try:
            identity = UUID(parts[1])
        except ValueError:
            raise KomaquillError(code="invalid_request", stage="storage") from None
        placement = self.read().runs.get(identity)
        if placement is None:
            raise FileNotFoundError(key)
        tail = PurePosixPath(*parts[2:])
        if placement.images is not None and len(tail.parts) == 2 and tail.parts[0] == "images":
            if not tail.stem.isdecimal() or int(tail.stem) < 1:
                raise KomaquillError(code="invalid_request", stage="storage")
            assert placement.scene is not None and placement.attempt is not None
            name = (
                f"{placement.scene:03d}-{placement.attempt:03d}-{int(tail.stem):03d}{tail.suffix}"
            )
            return self.path(f"{placement.images}/{name}")
        return self.path(f"{placement.folder}/{tail.as_posix()}")

    def rebuild(self) -> StorageIndex:
        """Derive placements from durable IDs, rejecting ambiguous ownership."""
        result = StorageIndex()
        for request_file in sorted((self.root / "stories").glob("*/request.json")):
            request = json.loads(request_file.read_bytes())
            identity = UUID(request["story_id"])
            folder = request_file.parent.relative_to(self.root).as_posix()
            if identity in result.stories:
                raise KomaquillError(code="identity_mismatch", stage="storage")
            result.stories[identity] = folder
            for render_file in sorted(request_file.parent.glob("renders/*/render.json")):
                render = json.loads(render_file.read_bytes())
                if (
                    UUID(render["story_id"]) != identity
                    or str(render["render_id"]) != render_file.parent.name
                ):
                    raise KomaquillError(code="identity_mismatch", stage="storage")
                render_folder = render_file.parent.relative_to(self.root).as_posix()
                for position, scene in enumerate(render["scenes"], 1):
                    for attempt, run_id in enumerate(scene["run_ids"], 1):
                        run = UUID(run_id)
                        if run in result.runs:
                            raise KomaquillError(code="identity_mismatch", stage="storage")
                        run_file = render_file.parent / "runs" / run_id / "run.json"
                        if (
                            run_file.is_file()
                            and json.loads(run_file.read_bytes())["run_id"] != run_id
                        ):
                            raise KomaquillError(code="identity_mismatch", stage="storage")
                        result.runs[run] = RunPlacement(
                            folder=f"{render_folder}/runs/{run}",
                            images=f"{render_folder}/images",
                            scene=position,
                            attempt=attempt,
                            recorded=run_file.is_file(),
                        )
        for run_file in sorted((self.root / "runs").glob("*/run.json")):
            value = json.loads(run_file.read_bytes())
            identity = UUID(value["run_id"])
            if str(identity) != run_file.parent.name or identity in result.runs:
                raise KomaquillError(code="identity_mismatch", stage="storage")
            result.runs[identity] = RunPlacement(
                folder=run_file.parent.relative_to(self.root).as_posix(), recorded=True
            )
        return result
