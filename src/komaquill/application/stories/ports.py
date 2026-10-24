"""Declare the narrow boundaries required by story use cases."""

from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from komaquill.domain.models import JsonObject
from komaquill.domain.stories import (
    PlannerProvenance,
    PlanningAttempt,
    PlanningResponse,
    StoryDraft,
    StoryInput,
    StoryPlan,
    StoryRender,
)


class StoryPlanner(Protocol):
    provenance: PlannerProvenance

    def complete(
        self, request: StoryInput, schema: dict[str, object], correction: str | None
    ) -> PlanningResponse: ...


class StoryRepository(Protocol):
    def begin(self, request: StoryInput) -> None: ...
    def prepare_attempt(
        self,
        request: StoryInput,
        provenance: PlannerProvenance,
        schema: dict[str, object],
        attempt: int,
        correction: str | None,
    ) -> None: ...
    def attempt(self, story_id: UUID, attempt: PlanningAttempt) -> None: ...
    def complete(self, plan: StoryPlan) -> None: ...
    def read(self, story_id: UUID) -> StoryPlan: ...
    def save_render(self, render: StoryRender) -> None: ...
    def render(self, render_id: UUID) -> StoryRender: ...
    def renders(self, story_id: UUID) -> list[StoryRender]: ...
    def claim(self, render_id: UUID) -> AbstractContextManager[bool]: ...


def output_schema(request: StoryInput) -> dict[str, object]:
    """Use the same schema for native output constraints and application validation."""
    schema = StoryDraft.model_json_schema()
    count = request.image_count
    schema["properties"]["scenes"].update(
        minItems=count if isinstance(count, int) else 1,
        maxItems=count if isinstance(count, int) else request.max_images,
    )
    return schema


def planner_input(
    request: StoryInput, provenance: PlannerProvenance, correction: str | None
) -> JsonObject:
    """Build reproducible model input without credentials or duplicated schemas."""
    return {
        "image_count": request.image_count,
        "max_images": request.max_images,
        "source_paragraphs": request.paragraphs,
        "drawing_policy": provenance.planning_policy,
        "correction": correction,
    }
