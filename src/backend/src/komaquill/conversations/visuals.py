"""Define reviewed physical descriptions separately from current clothing."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import Field

from komaquill.domain.roleplay import Record


class VisualPerson(Record):
    appearance: str = Field(max_length=3000)
    initial_clothing: str = Field(max_length=3000)
    current_clothing: str = Field(max_length=3000)


class VisualReview(Record):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    player: VisualPerson
    character: VisualPerson


class VisualReviewRecord(Record):
    request: VisualReview
    revision: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
