"""Persist story plans and render links without duplicating image state."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from filelock import FileLock, Timeout

from komaquill.application.stories.ports import planner_input
from komaquill.domain.errors import KomaquillError
from komaquill.domain.stories import (
    PlannerProvenance,
    PlanningAttempt,
    StoryInput,
    StoryPlan,
    StoryRender,
)
from komaquill.domain.workflow import digest
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.storage import atomic_json


class FileStoryRepository:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.root = project.data / "stories"

    def begin(self, request: StoryInput) -> None:
        folder = self.root / str(request.story_id)
        folder.mkdir(parents=True, exist_ok=False)
        atomic_json(folder / "request.json", request.model_dump(mode="json"))

    def prepare_attempt(
        self,
        request: StoryInput,
        provenance: PlannerProvenance,
        schema: dict[str, object],
        attempt: int,
        correction: str | None,
    ) -> None:
        """Commit the exact input and configuration before the model is called."""
        folder = self.root / str(request.story_id)
        if not (folder / "provenance.json").exists():
            atomic_json(folder / "provenance.json", provenance.model_dump(mode="json"))
        atomic_json(
            folder / "planning/inputs" / f"attempt-{attempt:02d}.json",
            {
                "format_version": 1,
                "schema": schema,
                "input": planner_input(request, provenance, correction),
            },
        )

    def attempt(self, story_id: UUID, attempt: PlanningAttempt) -> None:
        path = self.root / str(story_id) / "planning" / f"{attempt.attempt:02d}.json"
        if path.exists():
            raise KomaquillError(code="invalid_request", stage="story_planning")
        atomic_json(path, attempt.model_dump(mode="json"))

    def complete(self, plan: StoryPlan) -> None:
        path = self.root / str(plan.request.story_id) / "plan.json"
        if path.exists():
            raise KomaquillError(code="invalid_request", stage="story_planning")
        value = plan.model_dump(mode="json")
        atomic_json(path, {"format_version": 1, "plan": value, "sha256": digest(value)})

    def read(self, story_id: UUID) -> StoryPlan:
        value = json.loads((self.root / str(story_id) / "plan.json").read_bytes())
        if value.get("format_version") != 1 or digest(value["plan"]) != value["sha256"]:
            raise KomaquillError(code="storage_failed", stage="story_planning")
        plan = StoryPlan.model_validate(value["plan"])
        if plan.request.story_id != story_id:
            raise KomaquillError(code="storage_failed", stage="story_planning")
        return plan

    def planning_evidence(self, story_id: UUID) -> dict[str, object]:
        """Expose immutable inputs and outcomes, including failed planning attempts."""
        folder = self.root / str(story_id)
        return {
            "request": json.loads((folder / "request.json").read_bytes()),
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
        path = self.root / str(render.story_id) / "renders" / str(render.render_id) / "render.json"
        atomic_json(path, render.model_dump(mode="json"))

    def render(self, render_id: UUID) -> StoryRender:
        paths = list(self.root.glob(f"*/renders/{render_id}/render.json"))
        if len(paths) != 1:
            raise KomaquillError(code="request_not_found", stage="story_rendering")
        return self._read_render(paths[0])

    @staticmethod
    def _read_render(path: Path) -> StoryRender:
        render = StoryRender.model_validate_json(path.read_bytes())
        if path.parent.name != str(render.render_id) or path.parents[2].name != str(
            render.story_id
        ):
            raise KomaquillError(code="storage_failed", stage="story_rendering")
        return render

    def renders(self, story_id: UUID) -> list[StoryRender]:
        values = [
            self._read_render(path)
            for path in (self.root / str(story_id) / "renders").glob("*/render.json")
        ]
        return sorted(values, key=lambda value: (value.created_at, str(value.render_id)))

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
