"""Validate bounded planning attempts and preserve input before every model call."""

import hashlib
import json
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
    """Repair invalid content once; never retry authentication, transport or truncation."""
    if planner.provenance.provider != "fake" and not allow_real_llm:
        raise KomaquillError(code="real_llm_not_allowed", stage="story_planning", retryable=False)
    if repair_attempts not in {0, 1}:
        raise KomaquillError(code="invalid_configuration", stage="story_planning")
    if isinstance(request.image_count, int) and request.image_count > max_images:
        raise KomaquillError(code="invalid_story_count", stage="story_planning", retryable=False)
    if request.image_count == "auto" and request.max_images != max_images:
        raise KomaquillError(code="invalid_configuration", stage="story_planning")
    if (
        hashlib.sha256(planner.provenance.instructions.encode()).hexdigest()
        != planner.provenance.instruction_sha256
    ):
        raise KomaquillError(code="invalid_configuration", stage="story_planning")
    provenance = planner.provenance.model_copy(update={"repair_attempts": repair_attempts})
    repository.begin(request)
    schema = output_schema(request)
    correction = None
    for index in range(repair_attempts + 1):
        repository.prepare_attempt(request, provenance, schema, index + 1, correction)
        applied_correction = correction
        started = datetime.now(UTC)
        response = None
        draft = None
        failure = None
        terminal = None
        try:
            response = planner.complete(request, schema, correction)
            if (
                planner.provenance.require_model_match
                and response.model != planner.provenance.configured_model
            ):
                raise KomaquillError(
                    code="identity_mismatch", stage="story_planning", retryable=False
                )
            if response.refusal or response.finish_reason not in {None, "stop"}:
                raise KomaquillError(
                    code="story_plan_invalid",
                    stage="story_planning",
                    retryable=False,
                    details={"reason": "refusal" if response.refusal else "incomplete_output"},
                )
            candidate = StoryDraft.model_validate_json(response.content)
            validate_draft(candidate, request)
            draft = candidate
        except ValueError as error:
            failure = failure_info(
                KomaquillError(code="story_plan_invalid", stage="story_planning"), "story_planning"
            )
            issues = (
                error.errors(include_input=False, include_context=False, include_url=False)
                if isinstance(error, ValidationError)
                else [{"message": str(error)}]
            )
            correction = json.dumps(
                {
                    "validation_errors": issues,
                    "previous_output": response.content if response else None,
                },
                ensure_ascii=False,
            )
        except KomaquillError as error:
            failure = failure_info(error, "story_planning")
            terminal = error
        finally:
            repository.attempt(
                request.story_id,
                PlanningAttempt(
                    attempt=index + 1,
                    correction=applied_correction,
                    output_schema=schema,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    response=response,
                    failure=failure,
                ),
            )
        if terminal is not None:
            raise terminal
        if draft is not None:
            plan = StoryPlan(
                request=request,
                title=draft.title,
                synopsis=draft.synopsis,
                characters=draft.characters,
                supplements=draft.supplements,
                scenes=[
                    Scene(**scene.model_dump(), scene_id=uuid4(), position=i)
                    for i, scene in enumerate(draft.scenes, 1)
                ],
                planner=provenance,
            )
            repository.complete(plan)
            return repository.read(request.story_id)
    raise KomaquillError(
        code="story_plan_invalid",
        stage="story_planning",
        details={"story_id": str(request.story_id)},
        retryable=False,
    )
