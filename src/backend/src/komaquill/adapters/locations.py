"""Resolve logical storage keys using rebuildable local placement records."""

from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.adapters.paths import WorkspacePaths, artifact_path
from komaquill.domain.errors import KomaquillError

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


class MigrationProgress(BaseModel):
    status: str


class StorageIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    stories: dict[UUID, str] = Field(default_factory=dict)
    runs: dict[UUID, RunPlacement] = Field(default_factory=dict)


class Locations:
    """Keep placement knowledge out of generation, evaluation and public contracts."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project
        self.root = project.data
        self.index_file = self.root / "locations.json"
        self.migration_file = project.state / "story-storage-migration.json"

    def path(self, key: str) -> Path:
        return artifact_path(self.root, key)

    def read(self) -> StorageIndex:
        if self.migration_file.is_file():
            try:
                state = MigrationProgress.model_validate_json(self.migration_file.read_bytes())
            except ValueError:
                raise KomaquillError(code="storage_failed", stage="storage") from None
            if state.status != "complete":
                raise KomaquillError(code="migration_required", stage="storage")
        if self.index_file.is_file():
            try:
                value = StorageIndex.model_validate_json(self.index_file.read_bytes())
            except ValueError:
                raise KomaquillError(code="storage_failed", stage="storage") from None
            for folder in value.stories.values():
                self.path(folder)
            for row in value.runs.values():
                self.path(row.folder)
                if row.images:
                    self.path(row.images)
            return value
        if any(self.project.runs.glob("*/run.json")) or any(
            (self.root / "stories").glob("*/request.json")
        ):
            raise KomaquillError(code="migration_required", stage="storage")
        return StorageIndex()

    def story(self, identity: UUID) -> Path:
        folder = self.read().stories.get(identity)
        if folder is None:
            raise FileNotFoundError(str(identity))
        return self.path(folder)

    def run(self, identity: UUID) -> Path:
        row = self.read().runs.get(identity)
        if row is None:
            raise FileNotFoundError(str(identity))
        return self.path(row.folder)

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
