"""Publish typed story snapshots without filesystem or transport dependencies."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from komaquill.domain.errors import FailureInfo
from komaquill.domain.evaluations import Evaluation
from komaquill.domain.images import CompiledImageInput
from komaquill.domain.models import ImageMetadata, RunState
from komaquill.domain.stories import Character, Scene, StoryProp


class ArtifactView(BaseModel):
    key: str
    duplicate_of: str | None = None
    metadata: ImageMetadata | None = None


class SceneView(Scene):
    compiled_input: CompiledImageInput | None = None
    image_profile: str | None = None
    state: Literal["unstarted"] | RunState
    run_id: UUID | None
    attempt_ids: list[UUID]
    primary_image: ArtifactView | None
    images: list[ArtifactView]
    candidates: list[ArtifactView] = []
    requested_candidates: int | None = None
    failure: FailureInfo | None


class StorySource(BaseModel):
    name: str
    text: str
    sha256: str


class StorySnapshot(BaseModel):
    """Describe one rendering using IDs and portable artifact keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    format_version: Literal[2, 3] = 3
    story_id: UUID
    render_id: UUID | None
    title: str
    synopsis: str
    source: StorySource
    image_count: int
    requested_images: int | Literal["auto"]
    purpose: str
    case_id: str | None
    planner: dict[str, str]
    characters: list[Character]
    props: list[StoryProp] | None = None
    supplements: list[str]
    image_profile: str | None
    updated_at: datetime
    complete: bool
    finished: bool
    counts: dict[str, int]
    scenes: list[SceneView]
    evaluations: list[Evaluation] | None = None
    unreadable_evaluation_ids: list[str] = []


class StoryEvent(BaseModel):
    """Expose progress with the same snapshot used for explicit reads."""

    kind: Literal["progress", "finished"]
    snapshot: StorySnapshot
    publication_failure: FailureInfo | None = None


class StoryInterrupted(KeyboardInterrupt):
    """Carry durable identities when a caller interrupts planning or rendering."""

    def __init__(self, *, story_id: UUID | None = None, render_id: UUID | None = None) -> None:
        super().__init__()
        self.story_id = story_id
        self.render_id = render_id


class StorySummary(BaseModel):
    """Describe a work without exposing its full source or provider configuration."""

    story_id: UUID
    title: str
    created_at: datetime
    requested_images: int | Literal["auto"]
    image_count: int | None
    render_id: UUID | None
    saved_scenes: int
    saved_candidates: int | None = None
    state: Literal["unplanned", "planned", "incomplete", "complete"]
    purpose: str
    case_id: str | None


class StoryListing(BaseModel):
    stories: list[StorySummary]
    unreadable_ids: list[UUID]
