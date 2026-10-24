"""Build validated immutable plans before sending any image requests."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from komaquill.application.stories.ports import StoryPlanner, StoryRepository, output_schema
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.domain.stories import (
    PlanningAttempt,
    Scene,
    StoryDraft,
    StoryInput,
    StoryPlan,
    validate_draft,
)


def plan_story(
    repository: StoryRepository,
    planner: StoryPlanner,
    request: StoryInput,
    *,
    allow_real_llm: bool = False,
    repair_attempts: int = 1,
    max_images: int,
) -> StoryPlan:
    """Bound repair calls and persist visible evidence without diagnostic payloads."""
    if planner.provenance.provider != "fake" and not allow_real_llm:
        raise KomaquillError(code="real_llm_not_allowed", stage="story_planning", retryable=False)
    if repair_attempts not in {0, 1}:
        raise KomaquillError(code="invalid_configuration", stage="story_planning")
    if request.image_count > max_images:
        raise KomaquillError(code="invalid_story_count", stage="story_planning", retryable=False)
    if (
        hashlib.sha256(planner.provenance.instructions.encode()).hexdigest()
        != planner.provenance.instruction_sha256
    ):
        raise KomaquillError(code="invalid_configuration", stage="story_planning")
    repository.begin(request)
    schema = output_schema(request)
    correction = None
    for index in range(repair_attempts + 1):
        started = datetime.now(UTC)
        response = None
        try:
            response = planner.complete(request, schema, correction)
            if (
                planner.provenance.require_model_match
                and response.model != planner.provenance.configured_model
            ):
                raise KomaquillError(
                    code="identity_mismatch", stage="story_planning", retryable=False
                )
            if response.refusal:
                raise KomaquillError(
                    code="story_plan_invalid", stage="story_planning", retryable=False
                )
            if response.finish_reason not in {None, "stop"}:
                raise ValueError("The model response did not finish normally.")
            draft = StoryDraft.model_validate_json(response.content)
            validate_draft(draft, request)
        except (ValueError, ValidationError):
            failure = failure_info(
                KomaquillError(code="story_plan_invalid", stage="story_planning"),
                "story_planning",
            )
            repository.attempt(
                request.story_id,
                PlanningAttempt(
                    attempt=index + 1,
                    correction=correction,
                    output_schema=schema,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    response=response,
                    failure=failure,
                ),
            )
            correction = (
                "Return complete JSON with the exact scene count, "
                "valid references covering EVERY source paragraph, Japanese narrative "
                "through the ending, English appearance and visual_description, "
                "and all required fields. "
                "Every scene needs a DISTINCT visual moment. Split source paragraphs into smaller "
                "consecutive moments when needed. Never repeat or restart the story sequence."
            )
        except KomaquillError as error:
            repository.attempt(
                request.story_id,
                PlanningAttempt(
                    attempt=index + 1,
                    correction=correction,
                    output_schema=schema,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    failure=failure_info(error, "story_planning"),
                    response=response,
                ),
            )
            raise
        else:
            repository.attempt(
                request.story_id,
                PlanningAttempt(
                    attempt=index + 1,
                    correction=correction,
                    output_schema=schema,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    response=response,
                ),
            )
            plan = StoryPlan(
                request=request,
                title=draft.title,
                synopsis=draft.synopsis,
                characters=draft.characters,
                supplements=draft.supplements,
                scenes=[
                    Scene(**scene.model_dump(), scene_id=uuid4(), position=position)
                    for position, scene in enumerate(draft.scenes, 1)
                ],
                planner=planner.provenance,
            )
            repository.complete(plan)
            return plan
    raise KomaquillError(
        code="story_plan_invalid",
        stage="story_planning",
        details={"story_id": str(request.story_id)},
        retryable=False,
    )
