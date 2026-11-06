"""Define reviewed physical descriptions separately from current clothing."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import Field, model_validator

from komaquill.domain.roleplay import Record


class VisualPerson(Record):
    appearance: str = Field(max_length=3000)
    initial_clothing: str | None = Field(max_length=3000)
    current_clothing: str | None = Field(max_length=3000)


class VisualReview(Record):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    player: VisualPerson | None = None
    character: VisualPerson | None = None

    @model_validator(mode="after")
    def person_required(self) -> "VisualReview":
        if self.player is None and self.character is None:
            raise ValueError("A person requiring review is required.")
        return self


class VisualReviewRecord(Record):
    request: VisualReview
    revision: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
