"""Track one agreed meeting independently of the current scene."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from komaquill.domain.conversations.evidence import SourceSpan
from komaquill.domain.roleplay import MAX_PLACE_LENGTH, Record

MEETING_FIELDS = ("date", "time", "place")
MeetingText = Annotated[str, Field(min_length=1, max_length=MAX_PLACE_LENGTH)]


class MeetingAnswer(Record):
    status: Literal["confirmed", "unknown", "cancelled"]
    date: MeetingText | None = None
    time: MeetingText | None = None
    place: MeetingText | None = None


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


class MeetingChange(Record):
    kind: Literal["set", "cancel"]
    quote: str = Field(min_length=1, description="Exact user passage expressing this decision.")
    date: MeetingText | None = Field(description="Exact stated date or weekday, otherwise null.")
    time: MeetingText | None = Field(description="Exact stated time, otherwise null.")
    place: MeetingText | None = Field(description="Exact stated rendezvous, otherwise null.")

    def apply(self, plan: MeetingPlan | None, text: str, turn_id: UUID) -> MeetingPlan:
        span = SourceSpan.locate(text, self.quote)
        explicit = {
            name: value for name in MEETING_FIELDS if (value := getattr(self, name)) is not None
        }
        if self.kind == "cancel":
            if explicit:
                raise ValueError("A cancellation has no new meeting values.")
            return MeetingPlan(cancellation=MeetingSource(turn_id=turn_id, span=span))
        if not explicit:
            raise ValueError("A meeting decision must supply at least one value.")
        values = plan.model_dump() if plan is not None and not plan.cancellation else {}
        for name, value in explicit.items():
            part = SourceSpan.locate(self.quote, value)
            values[name] = MeetingValue(
                text=value,
                turn_id=turn_id,
                span=SourceSpan(start=span.start + part.start, end=span.start + part.end),
            )
        return MeetingPlan.model_validate(values)
