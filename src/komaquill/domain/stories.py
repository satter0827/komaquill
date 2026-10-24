"""Define portable story plans and image-render attempts."""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import FailureInfo


class StoryRecord(BaseModel):
    """Reject unknown fields and ambiguous timestamps in durable records."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*")
    @classmethod
    def nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Text fields must not be blank.")
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Timestamps require a timezone.")
        return value


class Character(StoryRecord):
    key: str
    name: str
    appearance: str = Field(
        description="English only: stable visible appearance, at most 25 words."
    )


class SceneDraft(StoryRecord):
    title: str
    narrative: str = Field(
        description=(
            "Japanese story prose. Together all scenes tell the whole source through its ending. "
            "For one scene, summarize the complete story."
        )
    )
    visual_description: str = Field(
        description=(
            "English only, at most 45 words. Describe one distinct still moment "
            "with composition and visible actions. Never write Japanese here."
        )
    )
    characters: list[str]
    location: str | None
    source_refs: list[str] = Field(
        description=(
            "Paragraph IDs narrated by this scene. Across all scenes cover every source paragraph. "
            "For one scene list ALL IDs."
        )
    )


class StoryDraft(StoryRecord):
    """Keep model output small and independent of image-engine settings."""

    title: str
    synopsis: str
    characters: list[Character]
    scenes: list[SceneDraft]
    supplements: list[str]


class StoryInput(StoryRecord):
    format_version: Literal[1] = 1
    story_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_name: str
    source_text: str
    source_sha256: str
    image_count: int = Field(ge=1)
    purpose: Literal["work", "verification"] = "work"
    case_id: str | None = None

    @property
    def paragraphs(self) -> dict[str, str]:
        parts = [
            text
            for text in re.split(r"\n\s*\n", self.source_text.replace("\r\n", "\n"))
            if text.strip()
        ]
        return {f"p{index}": text for index, text in enumerate(parts, 1)}

    @model_validator(mode="after")
    def valid_source(self) -> Self:
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("The source checksum does not match.")
        return self


class Scene(SceneDraft):
    scene_id: UUID
    position: int = Field(ge=1)


class PlanningResponse(BaseModel):
    """Keep visible output separate from credentials and wire responses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    refusal: bool = False
    content: str
    model: str | None = None
    finish_reason: str | None = None


class PlanningAttempt(StoryRecord):
    format_version: Literal[1] = 1
    attempt: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime
    response: PlanningResponse | None = None
    failure: FailureInfo | None = None
    correction: str | None = None
    output_schema: dict[str, Any] = Field(default_factory=dict)


class PlannerProvenance(StoryRecord):
    profile: str
    provider: str
    configured_model: str
    instructions: str
    instruction_sha256: str
    timeout_seconds: float
    max_output_tokens: int
    require_model_match: bool = False
    base_url: str | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    app_identity: dict[str, Any] = Field(default_factory=dict)


class StoryPlan(StoryRecord):
    format_version: Literal[1] = 1
    request: StoryInput
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str
    synopsis: str
    characters: list[Character]
    scenes: list[Scene]
    supplements: list[str]
    planner: PlannerProvenance

    @model_validator(mode="after")
    def references(self) -> Self:
        validate_draft(
            StoryDraft(
                title=self.title,
                synopsis=self.synopsis,
                characters=self.characters,
                scenes=[
                    SceneDraft.model_validate(scene.model_dump(exclude={"scene_id", "position"}))
                    for scene in self.scenes
                ],
                supplements=self.supplements,
            ),
            self.request,
        )
        if [scene.position for scene in self.scenes] != list(
            range(1, self.request.image_count + 1)
        ):
            raise ValueError("Scene positions must be consecutive.")
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Scene identifiers must be distinct.")
        return self


class SceneAttempts(StoryRecord):
    scene_id: UUID
    seed: int = Field(ge=0, le=2**64 - 1)
    run_ids: list[UUID] = Field(default_factory=list)


class StoryRender(StoryRecord):
    """Freeze image settings while retaining independent request attempts."""

    format_version: Literal[1] = 1
    render_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    plan_sha256: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    image_profile: str
    settings: RuntimeSettings
    scenes: list[SceneAttempts]

    @model_validator(mode="after")
    def distinct_requests(self) -> Self:
        identities = [value for scene in self.scenes for value in scene.run_ids]
        if len(identities) != len(set(identities)):
            raise ValueError("Image request identifiers must be distinct.")
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Scene identifiers must be distinct.")
        return self


def validate_draft(draft: StoryDraft, request: StoryInput) -> None:
    """Check product invariants in both fake and real model output."""
    if len(draft.scenes) != request.image_count:
        raise ValueError("The scene count differs from the requested image count.")
    descriptions = [" ".join(scene.visual_description.split()).casefold() for scene in draft.scenes]
    if len(set(descriptions)) != len(descriptions):
        raise ValueError("Every scene must depict a distinct visual moment.")
    keys = [character.key for character in draft.characters]
    if len(set(keys)) != len(keys):
        raise ValueError("Character keys must be distinct.")
    if {ref for scene in draft.scenes for ref in scene.source_refs} != set(request.paragraphs):
        raise ValueError("Every source paragraph must be represented by the plan.")
    for scene in draft.scenes:
        if not scene.source_refs or not set(scene.source_refs) <= request.paragraphs.keys():
            raise ValueError("Scene source references are invalid.")
        if not set(scene.characters) <= set(keys) or len(set(scene.characters)) != len(
            scene.characters
        ):
            raise ValueError("Scene character references are invalid.")


def image_prompt(plan: StoryPlan, scene: Scene) -> str:
    """Combine scene intent and stable appearance without model-specific quality tags."""
    characters = {character.key: character for character in plan.characters}
    return "\n".join(
        [
            *(characters[key].appearance for key in scene.characters),
            scene.visual_description,
        ]
    )
