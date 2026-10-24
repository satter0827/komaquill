"""Compose planning, rendering and evidence for all local interfaces."""

import hashlib
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from komaquill.api._errors import api_operation
from komaquill.application.generation import verify_runtime
from komaquill.application.stories.listing import list_stories
from komaquill.application.stories.planning import plan_story
from komaquill.application.stories.rendering import create_render, render_events, story_view
from komaquill.domain.errors import ErrorCode, KomaquillError, OperationStage, failure_info
from komaquill.domain.models import JsonObject, Session
from komaquill.domain.stories import StoryInput, StoryPlan
from komaquill.domain.story_views import StoryEvent, StoryInterrupted, StoryListing, StorySnapshot
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import operation_scope, record_event
from komaquill.infrastructure.sessions.connection import connected, session_project
from komaquill.infrastructure.stories.configuration import StorySettings, settings
from komaquill.infrastructure.stories.llm import planner, serialized_planning
from komaquill.infrastructure.stories.publication import publish_catalog, publish_story
from komaquill.infrastructure.stories.storage import FileStoryRepository


class StoryService:
    """Keep orchestration independent of terminal output and process exit codes."""

    def __init__(self, project: Project) -> None:
        self._project = project
        self._repository = FileStoryRepository(project)

    def _plan(
        self, text: str, source_name: str, options: StorySettings, allow_real_llm: bool
    ) -> StoryPlan:
        profile = options.llm_profiles[options.llm_profile]
        if profile.provider != "fake" and not allow_real_llm:
            raise KomaquillError(
                code="real_llm_not_allowed", stage="story_planning", retryable=False
            )
        with serialized_planning(self._project, options.request_timeout):
            request = StoryInput(
                source_name=source_name,
                source_text=text,
                source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                image_count=options.images,
                max_images=options.max_images,
                purpose="verification"
                if options.llm_profiles[options.llm_profile].provider == "fake"
                else options.purpose,
                case_id=options.case_id,
            )
            model = planner(self._project, options, allow_real_llm=allow_real_llm)
            started = time.monotonic()
            record_event(self._project, "story_planning", "started", story_id=request.story_id)
            try:
                result = plan_story(
                    self._repository,
                    model,
                    request,
                    allow_real_llm=allow_real_llm,
                    repair_attempts=options.repair_attempts,
                    max_images=options.max_images,
                )
            except KeyboardInterrupt:
                raise StoryInterrupted(story_id=request.story_id) from None
            except Exception as error:
                try:
                    publish_catalog(self._repository, self._project)
                except (OSError, ValueError, KomaquillError) as publication_error:
                    record_event(
                        self._project,
                        "story_publication",
                        "failed",
                        story_id=request.story_id,
                        failure=failure_info(publication_error, "finalize"),
                    )
                record_event(
                    self._project,
                    "story_planning",
                    "failed",
                    story_id=request.story_id,
                    elapsed_seconds=time.monotonic() - started,
                    failure=failure_info(error, "story_planning"),
                )
                if isinstance(error, KomaquillError):
                    error.details.setdefault("story_id", str(request.story_id))
                raise
            record_event(
                self._project,
                "story_planning",
                "saved",
                story_id=request.story_id,
                elapsed_seconds=time.monotonic() - started,
            )
            publish_story(self._repository, self._project, request.story_id)
            publish_catalog(self._repository, self._project)
            return result

    def plan(
        self,
        text: str,
        *,
        source_name: str = "story.txt",
        images: int | Literal["auto"] | None = None,
        llm_profile: str | None = None,
        image_profile: str | None = None,
        allow_real_llm: bool = False,
    ) -> StoryPlan:
        with (
            operation_scope(),
            api_operation(OperationStage.STORY_PLANNING, invalid=ErrorCode.INVALID_CONFIGURATION),
        ):
            options = settings(
                self._project, images=images, llm_profile=llm_profile, image_profile=image_profile
            )
            return self._plan(text, source_name, options, allow_real_llm)

    def list(self) -> StoryListing:
        with api_operation(OperationStage.STORAGE), self._repository.snapshot():
            return list_stories(self._repository, self._project)

    def show(self, story_id: UUID, *, render_id: UUID | None = None) -> StorySnapshot:
        with api_operation(OperationStage.STORAGE), self._repository.snapshot():
            return StorySnapshot.model_validate(
                story_view(self._repository, self._project, story_id, render_id)
            )

    def refresh(self, story_id: UUID, *, render_id: UUID | None = None) -> StorySnapshot:
        """Rebuild readable files without contacting a model or image backend."""
        with operation_scope(), api_operation(OperationStage.FINALIZE):
            self._repository.read(story_id)
            try:
                publish_story(self._repository, self._project, story_id, render_id)
                publish_catalog(self._repository, self._project)
            except KomaquillError as error:
                record_event(
                    self._project,
                    "story_publication",
                    "failed",
                    story_id=story_id,
                    render_id=render_id,
                    failure=failure_info(error, "finalize"),
                )
                raise
            return self.show(story_id, render_id=render_id)

    def follow(self, story_id: UUID, *, render_id: UUID | None = None) -> Iterator[StorySnapshot]:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            interval = session_project(self._project).settings().poll_interval
        previous = None
        while True:
            snapshot = self.show(story_id, render_id=render_id)
            if snapshot != previous:
                yield snapshot
                previous = snapshot
            if snapshot.finished:
                return
            time.sleep(interval)

    def export(self, story_id: UUID, *, render_id: UUID | None = None) -> JsonObject:
        with api_operation(OperationStage.STORAGE), self._repository.snapshot():
            evidence = self._repository.planning_evidence(story_id)
            try:
                plan = self._repository.read(story_id)
            except FileNotFoundError:
                plan = None
            return {
                "format_version": 2,
                "exported_at": datetime.now(UTC).isoformat(),
                "exporter": self._project.identity(),
                "story": self.show(story_id, render_id=render_id).model_dump(mode="json")
                if plan
                else None,
                "plan": plan.model_dump(mode="json", exclude_unset=True) if plan else None,
                "planning": evidence,
                "data_roles": {
                    "source": "original",
                    "narrative": "llm_retelling",
                    "character_appearance": "illustration_design",
                    "visual_description": "illustration_direction",
                    "supplements": "llm_declared_additions",
                    "source_fact_verification": "not_performed",
                },
            }

    def _events(
        self, backend: ComfyClient, session: Session, render_id: UUID, retry_failed: bool
    ) -> Iterator[StoryEvent]:
        try:
            for value in render_events(
                self._repository,
                self._project,
                backend,
                session,
                render_id,
                retry_failed=retry_failed,
            ):
                publish_story(self._repository, self._project, UUID(value["story_id"]), render_id)
                publish_catalog(self._repository, self._project)
                snapshot = StorySnapshot.model_validate(value)
                yield StoryEvent(
                    kind="finished" if snapshot.finished else "progress", snapshot=snapshot
                )
        except KeyboardInterrupt:
            raise StoryInterrupted(render_id=render_id) from None
        except KomaquillError as error:
            error.details.setdefault("render_id", str(render_id))
            raise

    def render(
        self, story_id: UUID, *, image_profile: str | None = None, seed: int | None = None
    ) -> Iterator[StoryEvent]:
        with (
            operation_scope(),
            api_operation(OperationStage.STORY_RENDERING),
            connected(self._project) as (backend, session),
        ):
            profile = image_profile or settings(self._project).image_profile
            render = create_render(
                self._repository, self._project, session, story_id, profile, seed
            )
            yield from self._events(backend, session, render.render_id, False)

    def resume(self, render_id: UUID, *, retry_failed: bool = False) -> Iterator[StoryEvent]:
        with (
            operation_scope(),
            api_operation(OperationStage.STORY_RENDERING),
            connected(self._project) as (backend, session),
        ):
            yield from self._events(backend, session, render_id, retry_failed)

    def generate(
        self,
        text: str,
        *,
        source_name: str = "story.txt",
        images: int | Literal["auto"] | None = None,
        llm_profile: str | None = None,
        image_profile: str | None = None,
        seed: int | None = None,
        allow_real_llm: bool = False,
    ) -> Iterator[StoryEvent]:
        with (
            operation_scope(),
            api_operation(OperationStage.STORY_PLANNING, invalid=ErrorCode.INVALID_CONFIGURATION),
        ):
            options = settings(
                self._project, images=images, llm_profile=llm_profile, image_profile=image_profile
            )
            if options.llm_profiles[options.llm_profile].provider != "fake" and not allow_real_llm:
                raise KomaquillError(
                    code="real_llm_not_allowed", stage="story_planning", retryable=False
                )
            with connected(self._project) as (backend, session):
                verify_runtime(backend, session)
                devices = backend.json("/system_stats").get("devices", [])
                if not isinstance(devices, list) or not any(
                    isinstance(d, dict) and d.get("type") and str(d["type"]).lower() != "cpu"
                    for d in devices
                ):
                    raise KomaquillError(code="image_backend_unavailable", stage="story_rendering")
                if (
                    session.settings is None
                    or options.image_profile not in session.settings.profiles
                ):
                    raise KomaquillError(code="invalid_configuration", stage="story_rendering")
                plan = self._plan(text, source_name, options, allow_real_llm)
                render = create_render(
                    self._repository,
                    self._project,
                    session,
                    plan.request.story_id,
                    options.image_profile,
                    seed,
                )
                yield from self._events(backend, session, render.render_id, False)
