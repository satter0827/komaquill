"""Define portable evidence for human and AI review without executing providers."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from komaquill.domain.errors import FailureInfo
from komaquill.domain.evaluations import Evaluation
from komaquill.domain.models import ImageMetadata, Run
from komaquill.domain.stories import Character, PlanningIssue, StoryProp, TokenUsage
from komaquill.domain.story_views import SceneView, StorySource

EvidenceDetail = Literal["summary", "full"]


class EvidenceRecord(BaseModel):
    key: str
    workspace_path: str
    sha256: str
    object_ref: str | None = None


class ImageObservation(BaseModel):
    observed_at: datetime
    sha256: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    valid: bool


class EvidenceImage(BaseModel):
    key: str
    workspace_path: str
    metadata: ImageMetadata | None
    observed: ImageObservation | None
    matches_record: bool | None


class EvidenceRun(BaseModel):
    run: Run
    images: list[EvidenceImage]
    workflow_integrity: bool | None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    inputs: list[dict[str, str]] = Field(default_factory=list)


class EvidenceAttempt(BaseModel):
    attempt: int
    started_at: datetime
    finished_at: datetime
    failure: FailureInfo | None
    validation_issues: list[PlanningIssue] | None
    model: str | None
    finish_reason: str | None
    usage: TokenUsage | None
    response_sha256: str | None


class PlanningEvidence(BaseModel):
    status: Literal["succeeded", "rejected", "failed", "interrupted", "unknown"]
    attempts: list[EvidenceAttempt]
    calls: int
    usage: TokenUsage
    elapsed_seconds: float | None
    repair_limit: int | None
    repair_limit_exhausted: bool | None
    failure: FailureInfo | None
    conditions: dict[str, JsonValue]
    producer: dict[str, str | None] | None
    unknown_fields: list[str]


class EvidenceStory(BaseModel):
    title: str
    synopsis: str
    requested_images: int | Literal["auto"]
    image_count: int
    purpose: str
    case_id: str | None
    render_id: UUID | None
    complete: bool
    finished: bool


class EvidenceRender(BaseModel):
    render_id: UUID
    created_at: datetime
    plan_sha256: str
    image_profile: str
    seeds: dict[str, int]


class StoryEvidence(BaseModel):
    """Expose one selection with optional content-addressed original records."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[3] = 3
    detail: EvidenceDetail
    exported_at: datetime
    exporter: dict[str, str | None]
    story_id: UUID
    source: StorySource
    story: EvidenceStory | None
    characters: list[Character]
    props: list[StoryProp] | None = None
    supplements: list[str]
    scenes: list[SceneView]
    planning: PlanningEvidence
    render: EvidenceRender | None
    selected_run_ids: list[UUID]
    runs: list[EvidenceRun]
    evaluations: list[Evaluation]
    unreadable_evaluation_ids: list[str] = Field(default_factory=list)
    assets: list[dict[str, JsonValue]]
    records: list[EvidenceRecord]
    objects: dict[str, JsonValue]
    data_roles: dict[str, str]
