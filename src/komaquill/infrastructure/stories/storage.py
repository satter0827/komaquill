"""Persist story plans and render links without duplicating image state."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field

from komaquill.application.stories.ports import planner_input
from komaquill.domain.errors import KomaquillError
from komaquill.domain.stories import (
    PlannerProvenance,
    PlanningAttempt,
    PlanningOutcome,
    StoryInput,
    StoryPlan,
    StoryRender,
)
from komaquill.domain.workflow import digest
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.locations import Locations, RunPlacement
from komaquill.infrastructure.stories.prompts import chat_messages


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


class FileStoryRepository:
    def __init__(self, project: Project) -> None:
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

    def begin(self, request: StoryInput) -> None:
        folder = Locations(self.project).register_story(
            request.story_id, request.source_name, request.created_at
        )
        folder.mkdir(parents=True, exist_ok=False)
        atomic_json(folder / "request.json", request.model_dump(mode="json"))
        from komaquill.infrastructure.atomic import atomic_bytes

        atomic_bytes(folder / "source.txt", request.source_text.encode("utf-8"))

    def prepare_attempt(
        self,
        request: StoryInput,
        provenance: PlannerProvenance,
        schema: dict[str, object],
        attempt: int,
        correction: str | None,
    ) -> None:
        """Commit the exact input and configuration before the model is called."""
        folder = self.folder(request.story_id)
        if not (folder / "provenance.json").exists():
            atomic_json(folder / "provenance.json", provenance.model_dump(mode="json"))
        atomic_json(
            folder / "planning/inputs" / f"attempt-{attempt:02d}.json",
            {
                "format_version": 2,
                "schema": schema,
                "input": planner_input(request, provenance, correction),
                "messages": chat_messages(request, provenance, correction),
            },
        )

    def attempt(self, story_id: UUID, attempt: PlanningAttempt) -> None:
        path = self.folder(story_id) / "planning" / f"{attempt.attempt:02d}.json"
        if path.exists():
            raise KomaquillError(code="invalid_request", stage="story_planning")
        atomic_json(path, attempt.model_dump(mode="json"))

    def finish_planning(self, story_id: UUID, outcome: PlanningOutcome) -> None:
        path = self.folder(story_id) / "planning-result.json"
        if path.exists():
            raise KomaquillError(code="invalid_request", stage="story_planning")
        atomic_json(path, outcome.model_dump(mode="json"))

    def complete(self, plan: StoryPlan) -> None:
        path = self.folder(plan.request.story_id) / "plan.json"
        if path.exists():
            raise KomaquillError(code="invalid_request", stage="story_planning")
        value = plan.model_dump(mode="json")
        atomic_json(path, {"format_version": 1, "plan": value, "sha256": digest(value)})

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

    def save_render(self, render: StoryRender) -> None:
        path = self.folder(render.story_id) / "renders" / str(render.render_id) / "render.json"
        locations = Locations(self.project)
        relative = path.parent.relative_to(self.project.data).as_posix()
        for identity, placement in render_placements(render, relative):
            locations.register_run(identity, placement)
        atomic_json(path, render.model_dump(mode="json"))

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

    def render_for_run(self, run_id: UUID) -> StoryRender | None:
        """Resolve story ownership at the storage boundary."""
        locations = Locations(self.project)
        placement = locations.read().runs.get(run_id)
        if placement is None or placement.images is None:
            return None
        render = self._read_render(locations.path(placement.folder).parents[1] / "render.json")
        if not any(run_id in scene.run_ids for scene in render.scenes):
            raise KomaquillError(code="identity_mismatch", stage="storage")
        return render

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        with Locations(self.project).snapshot():
            yield

    @contextmanager
    def claim(self, render_id: UUID) -> Iterator[bool]:
        folder = self.project.state / "stories"
        folder.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(folder / f"{render_id}.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            yield False
            return
        try:
            yield True
        finally:
            lock.release()
