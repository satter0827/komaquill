"""Describe explicit repairs without weakening normal image completion guards."""

import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from komaquill.domain.library.models import CharacterInput, CreationJob, Preset, StoryInput
from komaquill.domain.library.rendering import preset_image_input
from komaquill.domain.roleplay import Record

RepairReason = Literal["ready", "no_candidate", "ambiguous", "invalid_image", "input_changed"]


class ImageLinkFinding(Record):
    target_id: UUID
    kind: Literal["character", "story"]
    candidates: list[UUID]
    reason: RepairReason


class ImageLinkInspection(Record):
    id: UUID = Field(default_factory=uuid4)
    findings: list[ImageLinkFinding]


class ImageLinkRepairResult(Record):
    id: UUID
    repaired: list[UUID]
    already_applied: list[UUID]
    skipped: dict[UUID, str]


def matches_image_input(
    kind: Literal["character", "story"], target: Preset, job: CreationJob
) -> bool:
    """Require the recorded identity, revision, purpose and exact resolved visual input."""
    if not (
        job.request.kind == ("portrait" if kind == "character" else "banner")
        and job.request.target_id == target.id
        and job.request.expected_revision == target.revision
    ):
        return False
    try:
        original = json.loads(job.original)
        expected = preset_image_input(kind, target)
        if original == expected:
            return True
        # Full retained inputs are interpreted only for this explicit maintenance operation.
        source = (CharacterInput if kind == "character" else StoryInput).model_validate_json(
            job.original, strict=True
        )
        return preset_image_input(kind, source) == expected
    except ValueError:
        return False
