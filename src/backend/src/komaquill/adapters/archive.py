"""Persist story plans and render links without duplicating image state."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from komaquill.adapters.locations import Locations, RunPlacement
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.archive.models import (
    PlanningOutcome,
    StoryInput,
    StoryPlan,
    StoryRender,
)
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Run
from komaquill.domain.workflow import digest


class StoredPlan(BaseModel):
    """Validate the storage envelope before inspecting its original plan payload."""

    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1]
    plan: dict[str, Any]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def render_placements(render: StoryRender, relative: str) -> Iterator[tuple[UUID, RunPlacement]]:
    for position, scene in enumerate(render.scenes, 1):
        for attempt, identity in enumerate(scene.run_ids, 1):
            yield (
                identity,
                RunPlacement(
                    folder=f"{relative}/runs/{identity}",
                    images=f"{relative}/images",
                    scene=position,
                    attempt=attempt,
                ),
            )


def invalid_story(story_id: UUID) -> KomaquillError:
    return KomaquillError(
        code="storage_failed",
        stage="storage",
        retryable=False,
        details={"story_id": str(story_id)},
    )


class ArchiveRepository:
    def __init__(self, project: RuntimeWorkspace) -> None:
        self.project = project
        self.root = project.data / "stories"

    def ids(self) -> tuple[UUID, ...]:
        locations = Locations(self.project)
        stories = locations.read().stories
        known = {locations.path(folder) for folder in stories.values()}
        if any(path.parent not in known for path in self.root.glob("*/request.json")):
            raise KomaquillError(
                code="storage_failed",
                stage="storage",
                retryable=False,
                details={"record": "story-index"},
            )
        return tuple(stories)

    def request(self, story_id: UUID) -> StoryInput:
        folder = self.folder(story_id)
        try:
            request = StoryInput.model_validate_json((folder / "request.json").read_bytes())
            if request.story_id != story_id:
                raise ValueError("The stored request identity does not match.")
            return request
        except (OSError, ValueError):
            raise invalid_story(story_id) from None

    def folder(self, story_id: UUID) -> Path:
        return Locations(self.project).story(story_id)

    def read(self, story_id: UUID) -> StoryPlan:
        folder = self.folder(story_id)
        try:
            try:
                content = (folder / "plan.json").read_bytes()
            except FileNotFoundError:
                outcome = folder / "planning-result.json"
                if (
                    outcome.is_file()
                    and PlanningOutcome.model_validate_json(outcome.read_bytes()).status
                    == "succeeded"
                ):
                    raise ValueError("The completed plan is missing.") from None
                raise
            value = StoredPlan.model_validate_json(content)
            if digest(value.plan) != value.sha256:
                raise ValueError("The stored plan checksum does not match.")
            plan = StoryPlan.model_validate(value.plan)
            if plan.request != self.request(story_id):
                raise ValueError("The plan differs from its retained request.")
            return plan
        except FileNotFoundError:
            raise
        except (OSError, ValueError):
            raise invalid_story(story_id) from None

    def planning_evidence(self, story_id: UUID) -> dict[str, object]:
        """Expose immutable inputs and outcomes, including failed planning attempts."""
        folder = self.folder(story_id)
        return {
            "request": self.request(story_id).model_dump(mode="json", exclude_unset=True),
            "outcome": json.loads((folder / "planning-result.json").read_bytes())
            if (folder / "planning-result.json").is_file()
            else None,
            "provenance": json.loads((folder / "provenance.json").read_bytes())
            if (folder / "provenance.json").is_file()
            else None,
            "inputs": [
                json.loads(path.read_bytes())
                for path in sorted((folder / "planning/inputs").glob("attempt-*.json"))
            ],
            "attempts": [
                json.loads(path.read_bytes())
                for path in sorted((folder / "planning").glob("[0-9]*.json"))
            ],
        }

    def render(self, render_id: UUID) -> StoryRender:
        paths = list(self.root.glob(f"*/renders/{render_id}/render.json"))
        if len(paths) != 1:
            raise KomaquillError(code="request_not_found", stage="story_rendering")
        return self._read_render(paths[0])

    def _read_render(self, path: Path) -> StoryRender:
        render = StoryRender.model_validate_json(path.read_bytes())
        if path.parent.name != str(render.render_id) or path.parents[2] != self.folder(
            render.story_id
        ):
            raise KomaquillError(code="storage_failed", stage="story_rendering")
        placements = Locations(self.project).read().runs
        relative = path.parent.relative_to(self.project.data).as_posix()
        for identity, expected in render_placements(render, relative):
            actual = placements.get(identity)
            if actual is None or actual.model_copy(update={"recorded": False}) != expected:
                raise KomaquillError(
                    code="storage_failed",
                    stage="storage",
                    retryable=False,
                    details={"render_id": str(render.render_id), "run_id": str(identity)},
                )
        return render

    def renders(self, story_id: UUID) -> list[StoryRender]:
        values = [
            self._read_render(path)
            for path in (self.folder(story_id) / "renders").glob("*/render.json")
        ]
        return sorted(values, key=lambda value: (value.created_at, str(value.render_id)))


def load_archived_runs(
    project: WorkspacePaths, *, unreadable: list[UUID | str] | None = None
) -> list[Run]:
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
