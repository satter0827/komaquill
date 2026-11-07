"""Track one agreed meeting independently of the current scene."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from komaquill.domain.conversations.evidence import SourceSpan
from komaquill.domain.roleplay import MAX_PLACE_LENGTH, Record

MEETING_FIELDS = ("date", "time", "place")


class MeetingValues(Record):
    date: str | None = Field(default=None, max_length=MAX_PLACE_LENGTH)
    time: str | None = Field(default=None, max_length=MAX_PLACE_LENGTH)
    place: str | None = Field(default=None, max_length=MAX_PLACE_LENGTH)


class MeetingAnswer(MeetingValues):
    status: Literal["confirmed", "unknown", "cancelled"]


class MeetingSource(Record):
    turn_id: UUID
    span: SourceSpan


class MeetingValue(MeetingSource):
    text: str = Field(min_length=1, max_length=MAX_PLACE_LENGTH)


class MeetingPlan(Record):
    date: MeetingValue | None = None
    time: MeetingValue | None = None
    place: MeetingValue | None = None
    cancellation: MeetingSource | None = None

    def answer(self) -> MeetingAnswer:
        if self.cancellation is not None:
            return MeetingAnswer(status="cancelled")
        return MeetingAnswer.model_validate(
            {
                "status": "confirmed",
                **{name: value.text for name in MEETING_FIELDS if (value := getattr(self, name))},
            }
        )


class MeetingChange(MeetingValues):
    kind: Literal["confirm", "correct", "cancel"]
    quote: str = Field(min_length=1, description="Exact user passage expressing this decision.")

    @model_validator(mode="after")
    def supplied_values(self) -> Self:
        values = [getattr(self, name) for name in MEETING_FIELDS]
        if any(value is not None and not value.strip() for value in values):
            raise ValueError("Meeting values must be nonempty.")
        if any(value is not None for value in values) == (self.kind == "cancel"):
            raise ValueError("Supply only the explicitly set values, or cancel without values.")
        return self

    def apply(self, plan: MeetingPlan | None, text: str, turn_id: UUID) -> MeetingPlan:
        span = SourceSpan.locate(text, self.quote)
        if self.kind == "cancel":
            return MeetingPlan(cancellation=MeetingSource(turn_id=turn_id, span=span))
        if self.kind == "correct" and (plan is None or plan.cancellation is not None):
            raise ValueError("A correction requires an active meeting.")
        values = plan.model_dump() if plan is not None and not plan.cancellation else {}
        for name in MEETING_FIELDS:
            value = getattr(self, name)
            if value is not None:
                part = SourceSpan.locate(self.quote, value)
                values[name] = MeetingValue(
                    text=value,
                    turn_id=turn_id,
                    span=SourceSpan(start=span.start + part.start, end=span.start + part.end),
                )
        return MeetingPlan.model_validate(values)
