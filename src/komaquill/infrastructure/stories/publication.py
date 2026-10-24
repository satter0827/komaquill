"""Rebuild readable story documents from immutable plans and current run records."""

import html
import json
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

from filelock import FileLock

from komaquill.application.stories.listing import list_stories
from komaquill.application.stories.ports import StoryRepository
from komaquill.application.stories.rendering import story_view
from komaquill.domain.errors import FailureInfo, KomaquillError, failure_info
from komaquill.domain.stories import StoryPlan, StoryRender
from komaquill.infrastructure.atomic import atomic_bytes, atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.locations import Locations
from komaquill.infrastructure.stories.storage import FileStoryRepository
from komaquill.messages import message


class PublicationSource(StoryRepository, Protocol):
    def read(self, story_id: UUID) -> StoryPlan: ...
    def folder(self, story_id: UUID) -> Path: ...
    def renders(self, story_id: UUID) -> list[StoryRender]: ...


def prose(value: object) -> str:
    """Render user prose as text, preventing remote embeds and injected headings."""
    return re.sub(r"([\\`*_{}\[\]#])", r"\\\1", html.escape(str(value)))


def write_document(path: Path, lines: list[str]) -> None:
    content = ("\n\n".join(lines) + "\n").encode("utf-8")
    if not path.is_file() or path.read_bytes() != content:
        atomic_bytes(path, content)


def _publish_story(
    repository: PublicationSource, project: Project, story_id: UUID, render_id: UUID | None = None
) -> None:
    """Publish local links only after images are committed; keep state in the run repository."""
    plan = repository.read(story_id)
    root = repository.folder(story_id)
    overview = [f"# {prose(plan.title)}", prose(plan.synopsis)]
    for render in repository.renders(story_id):
        link = f"renders/{render.render_id}/story.md"
        overview.append(f"- [{render.created_at.isoformat()} — {render.render_id}]({link})")
    write_document(root / "README.md", overview)
    snapshot = story_view(repository, project, story_id, render_id)
    if snapshot["render_id"] is None:
        return
    render_folder = root / "renders" / str(snapshot["render_id"])
    lines = [f"# {prose(snapshot['title'])}", prose(snapshot["synopsis"])]
    for scene in snapshot["scenes"]:
        lines.append(f"## {scene['position']:03d} — {prose(scene['title'])}")
        lines.append(prose(scene["narrative"]))
        lines.append(message("stories.document_state", state=scene["state"]))
        image = scene["primary_image"]
        if image is not None:
            target = project.artifacts_store.path(image["key"])
            if not target.is_file():
                raise KomaquillError(
                    code="storage_failed",
                    stage="finalize",
                    details={"render_id": str(snapshot["render_id"])},
                )
            relative = target.relative_to(render_folder).as_posix()
            lines.append(f"![{scene['position']:03d}]({quote(relative, safe='/')})")
    path = render_folder / "manifest.json"
    try:
        unchanged = json.loads(path.read_bytes()) == snapshot
    except (OSError, ValueError):
        unchanged = False
    if not unchanged:
        atomic_json(path, snapshot)
    write_document(render_folder / "story.md", lines)


def publish_listing(repository: PublicationSource, project: Project) -> None:
    """Make the storage root a readable entrypoint, including unfinished plans."""
    listing = list_stories(repository, project)
    root = project.data / "stories"
    lines = [message("stories.catalog_heading")]
    for row in listing.stories:
        folder = repository.folder(row.story_id)
        if not (folder / "README.md").exists():
            write_document(
                folder / "README.md", [f"# {prose(row.title)}", message("stories.plan_unavailable")]
            )
        key = (folder.relative_to(root) / "README.md").as_posix()
        count = row.image_count if row.image_count is not None else "?"
        lines.append(
            f"- [{prose(row.title)}]({quote(key, safe='/')}) — {row.created_at.isoformat()}"
            f" — {row.state} — {row.saved_images}/{count} — {row.purpose}"
        )
    for identity in listing.unreadable_ids:
        lines.append(message("stories.catalog_unreadable", story_id=identity))
    write_document(root / "README.md", lines)


def publish_story(
    repository: PublicationSource, project: Project, story_id: UUID, render_id: UUID | None = None
) -> None:
    """Serialize projection reads and writes so older snapshots cannot win a race."""
    project.state.mkdir(parents=True, exist_ok=True)
    try:
        with (
            FileLock(str(project.state / f".story-publication-{story_id}.lock")),
            Locations(project).snapshot(fresh=True),
        ):
            _publish_story(repository, project, story_id, render_id)
    except (OSError, ValueError):
        raise KomaquillError(
            code="publication_failed",
            stage="finalize",
            details={"story_id": str(story_id)},
        ) from None


def publish_catalog(repository: PublicationSource, project: Project) -> None:
    project.state.mkdir(parents=True, exist_ok=True)
    with (
        FileLock(str(project.state / ".story-listing.lock")),
        Locations(project).snapshot(fresh=True),
    ):
        publish_listing(repository, project)


class PublicationTracker:
    """Observe canonical run revisions and retry only affected local projections."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.repository = FileStoryRepository(project)
        self.observed: dict[UUID, str] = {}
        self.unresolved: set[UUID] = set()
        self.pending: set[tuple[UUID, UUID]] = set()
        self.failures: dict[str, FailureInfo] = {}
        self.catalog_pending = False

    def _failed(self, identity: UUID | str, error: BaseException) -> None:
        failure = failure_info(error, "finalize")
        key = str(identity)
        if self.failures.get(key) != failure:
            record_event(
                self.project,
                "story_publication",
                "failed",
                story_id=identity if isinstance(identity, UUID) else None,
                failure=failure,
            )
        self.failures[key] = failure

    def reconcile(self) -> tuple[UUID | str, ...]:
        try:
            return self._reconcile()
        except (OSError, ValueError, KomaquillError) as error:
            self._failed("index", error)
            return ("index",)

    def _reconcile(self) -> tuple[UUID | str, ...]:
        """Reconstruct work after restart without a persistent retry queue."""
        self.catalog_pending = True
        with self.repository.snapshot():
            for identity in self.repository.ids():
                try:
                    if (self.repository.folder(identity) / "plan.json").is_file():
                        self.repository.read(identity)
                        self.failures.pop(str(identity), None)
                        renders = self.repository.renders(identity)
                        if not renders:
                            publish_story(self.repository, self.project, identity)
                        self.pending.update((identity, render.render_id) for render in renders)
                except (OSError, ValueError, KomaquillError) as error:
                    self._failed(identity, error)
        return self.tick(())

    def tick(self, versions: tuple[tuple[UUID, str], ...]) -> tuple[UUID | str, ...]:
        try:
            self.failures.pop("index", None)
            result = self._tick(versions)
            return result
        except (OSError, ValueError, KomaquillError) as error:
            self._failed("index", error)
            return ("index",)

    def _tick(self, versions: tuple[tuple[UUID, str], ...]) -> tuple[UUID | str, ...]:
        for identity, revision in versions:
            if self.observed.get(identity) != revision:
                self.unresolved.add(identity)
            self.observed[identity] = revision
        with self.repository.snapshot():
            for identity in tuple(self.unresolved):
                try:
                    render = self.repository.render_for_run(identity)
                    if render is not None:
                        self.pending.add((render.story_id, render.render_id))
                    self.unresolved.remove(identity)
                    self.failures.pop(str(identity), None)
                except (OSError, ValueError, KomaquillError) as error:
                    self._failed(identity, error)
        for story_id, render_id in sorted(self.pending):
            try:
                publish_story(self.repository, self.project, story_id, render_id)
                self.pending.remove((story_id, render_id))
                self.failures.pop(str(story_id), None)
                self.catalog_pending = True
            except (OSError, ValueError, KomaquillError) as error:
                self._failed(story_id, error)
        if self.catalog_pending:
            try:
                publish_catalog(self.repository, self.project)
                self.catalog_pending = False
                self.failures.pop("catalog", None)
            except (OSError, ValueError, KomaquillError) as error:
                self._failed("catalog", error)
        return tuple(
            UUID(key) if key != "catalog" and key != "index" else key
            for key in sorted(self.failures)
        )


def refresh_saved_stories(project: Project) -> tuple[UUID | str, ...]:
    return PublicationTracker(project).reconcile()
