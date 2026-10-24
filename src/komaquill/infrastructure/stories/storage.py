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
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.locations import Locations, RunPlacement


class FileStoryRepository:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.root = project.data / "stories"

    def ids(self) -> tuple[UUID, ...]:
        return tuple(Locations(self.project).read().stories)

    def request(self, story_id: UUID) -> StoryInput:
        return StoryInput.model_validate_json((self.folder(story_id) / "request.json").read_bytes())

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
        from komaquill.infrastructure.stories.publication import publish_listing

        with FileLock(str(self.project.state / ".story-listing.lock")):
            publish_listing(self, self.project)

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
                "format_version": 1,
                "schema": schema,
                "input": planner_input(request, provenance, correction),
            },
        )

    def attempt(self, story_id: UUID, attempt: PlanningAttempt) -> None:
        path = self.folder(story_id) / "planning" / f"{attempt.attempt:02d}.json"
        if path.exists():
            raise KomaquillError(code="invalid_request", stage="story_planning")
        atomic_json(path, attempt.model_dump(mode="json"))

    def complete(self, plan: StoryPlan) -> None:
        path = self.folder(plan.request.story_id) / "plan.json"
        if path.exists():
            raise KomaquillError(code="invalid_request", stage="story_planning")
        value = plan.model_dump(mode="json")
        atomic_json(path, {"format_version": 1, "plan": value, "sha256": digest(value)})
        self.publish(plan.request.story_id)

    def read(self, story_id: UUID) -> StoryPlan:
        value = json.loads((self.folder(story_id) / "plan.json").read_bytes())
        if value.get("format_version") != 1 or digest(value["plan"]) != value["sha256"]:
            raise KomaquillError(code="storage_failed", stage="story_planning")
        plan = StoryPlan.model_validate(value["plan"])
        if plan.request.story_id != story_id:
            raise KomaquillError(code="storage_failed", stage="story_planning")
        return plan

    def planning_evidence(self, story_id: UUID) -> dict[str, object]:
        """Expose immutable inputs and outcomes, including failed planning attempts."""
        folder = self.folder(story_id)
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
        path = self.folder(render.story_id) / "renders" / str(render.render_id) / "render.json"
        locations = Locations(self.project)
        relative = path.parent.relative_to(self.project.data).as_posix()
        for position, scene in enumerate(render.scenes, 1):
            for attempt, identity in enumerate(scene.run_ids, 1):
                locations.register_run(
                    identity,
                    RunPlacement(
                        folder=f"{relative}/runs/{identity}",
                        images=f"{relative}/images",
                        scene=position,
                        attempt=attempt,
                    ),
                )
        atomic_json(path, render.model_dump(mode="json"))
        self.publish(render.story_id, render.render_id)

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
        return render

    def renders(self, story_id: UUID) -> list[StoryRender]:
        values = [
            self._read_render(path)
            for path in (self.folder(story_id) / "renders").glob("*/render.json")
        ]
        return sorted(values, key=lambda value: (value.created_at, str(value.render_id)))

    def publish(self, story_id: UUID, render_id: UUID | None = None) -> None:
        from komaquill.infrastructure.stories.publication import publish_story

        try:
            publish_story(self, self.project, story_id, render_id)
        except (OSError, ValueError):
            raise KomaquillError(
                code="publication_failed",
                stage="finalize",
                details={
                    "story_id": str(story_id),
                    **({"render_id": str(render_id)} if render_id else {}),
                },
            ) from None

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


def refresh_saved_stories(project: Project) -> tuple[UUID, ...]:
    """Retry local projections independently of collection and GPU shutdown."""
    from komaquill.domain.errors import failure_info
    from komaquill.infrastructure.diagnostics import record_event

    failed = []
    repository = FileStoryRepository(project)
    for identity in Locations(project).read().stories:
        try:
            if (repository.folder(identity) / "plan.json").is_file():
                for render in repository.renders(identity):
                    repository.publish(identity, render.render_id)
        except (OSError, ValueError, KomaquillError) as error:
            failed.append(identity)
            record_event(
                project,
                "story_publication",
                "failed",
                story_id=identity,
                failure=failure_info(error, "finalize"),
            )
    return tuple(failed)
