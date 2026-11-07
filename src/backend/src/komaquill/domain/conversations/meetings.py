"""Track one agreed meeting independently of the current scene."""

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from komaquill.domain.conversations.evidence import (
    SourceSpan,
    has_separate_request,
    is_direct_statement,
    is_quoted,
)
from komaquill.domain.roleplay import MAX_PLACE_LENGTH, Record

MEETING_FIELDS = ("date", "time", "place")
MeetingText = Annotated[str, Field(min_length=1, max_length=MAX_PLACE_LENGTH)]
MEETING_TOPIC = re.compile(r"\b(?:meet|meeting|rendezvous)\b|集合|待ち合わせ", re.IGNORECASE)
_CANCELLATION = re.compile(
    r"\b(?:cancel(?:ling|ing)?|call(?:ing)? off) (?:the |our |this |that )?(?:meeting|rendezvous)\b"
    r"|\b(?:meeting|rendezvous) (?:is |has been )?(?:cancelled|canceled|called off)\b"
    r"|(?:集合|待ち合わせ)(?:予定)?[はをが、\s]*(?:中止|取り消|取消)",
    re.IGNORECASE,
)
_NEGATED_CANCELLATION = re.compile(
    r"\b(?:not|never|don't|won't|can't|cannot)\b|しない|しません|せず|ではない",
    re.IGNORECASE,
)
_CORRECTION = re.compile(r"\b(?:change|correct|reschedule)\b|変更|訂正", re.IGNORECASE)
_IMPLICIT_CORRECTION = re.compile(
    r"^\s*(?:change|correct) (?:(?:only|just) )?the (?:date|day|time|place|location)"
    r"(?: (?:only|just))? to\b"
    r"|^\s*(?:日付|曜日|時刻|時間|場所)(?:だけ)?(?:を|は)?[^。!?]+に(?:変更|訂正)",
    re.IGNORECASE,
)
_NEGATED_UPDATE = re.compile(
    r"\b(?:not|never|don['’]t|won['’]t) (?:change|correct|reschedule|set|move|meet)\b"
    r"|(?:変更|訂正|確定|集合|待ち合わせ)(?:は|を)?(?:しない|しません|せず)",
    re.IGNORECASE,
)
_DECISION = re.compile(
    r"\b(?:will meet|are meeting|meet (?:on|at)|let['’]s meet|we meet|is|set|confirm)\b"
    r"|決め|確定|しましょう|にします|待ち合わせ(?:る|ます)|集合(?:する|します)",
    re.IGNORECASE,
)
_QUERY_TOPIC = re.compile(
    r"\b(?:plan|details|status|decid(?:e|ed)|recap|summari[sz]e)\b|予定|決めたこと|確定事項",
    re.IGNORECASE,
)
_INQUIRY = re.compile(
    r"[?？]|\b(?:what|when|where|confirm|check|recap|summari[sz]e|tell me)\b"
    r"|教えて|確認|振り返|まとめ",
    re.IGNORECASE,
)


def meeting_query_source(
    text: str, quote: str, has_plan: bool, *, excluded: SourceSpan | None = None
) -> SourceSpan:
    source = SourceSpan.locate(text, quote)
    topic = MEETING_TOPIC.search(quote) or (_QUERY_TOPIC.search(quote) if has_plan else None)
    if (
        (excluded is not None and source.overlaps(excluded))
        or not source.is_single_sentence(text)
        or is_quoted(text, source)
        or not _INQUIRY.search(quote)
        or topic is None
        or has_separate_request(quote[topic.end() :])
    ):
        raise ValueError("A meeting inquiry must refer to the meeting or retained decisions.")
    return source


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

    def apply(
        self,
        plan: MeetingPlan | None,
        text: str,
        turn_id: UUID,
        *,
        excluded: SourceSpan | None = None,
    ) -> MeetingPlan:
        span = SourceSpan.locate(text, self.quote)
        if (excluded is not None and span.overlaps(excluded)) or not is_direct_statement(
            text, span
        ):
            raise ValueError("A meeting decision must be a direct statement outside a move.")
        if has_separate_request(self.quote):
            raise ValueError("A meeting decision cannot include a separate request.")
        explicit = {
            name: value for name in MEETING_FIELDS if (value := getattr(self, name)) is not None
        }
        if self.kind == "cancel":
            if (
                explicit
                or not _CANCELLATION.search(self.quote)
                or _NEGATED_CANCELLATION.search(span.context(text))
            ):
                raise ValueError(
                    "A cancellation requires an explicit decision to call off the meeting."
                )
            return MeetingPlan(cancellation=MeetingSource(turn_id=turn_id, span=span))
        if not explicit:
            raise ValueError("A meeting decision must supply at least one value.")
        current = plan if plan is not None and plan.cancellation is None else None
        context = span.context(text)
        addressed = MEETING_TOPIC.search(self.quote) and (
            _DECISION.search(self.quote) or _CORRECTION.search(self.quote)
        )
        if _NEGATED_UPDATE.search(context) or not (
            addressed or (current and _IMPLICIT_CORRECTION.search(context))
        ):
            raise ValueError("A meeting update must identify the meeting or correct its values.")
        values = current.model_dump() if current is not None else {}
        for name, value in explicit.items():
            retained = getattr(current, name) if current is not None else None
            if retained is not None and retained.text == value:
                continue
            part = SourceSpan.locate(self.quote, value)
            values[name] = MeetingValue(
                text=value,
                turn_id=turn_id,
                span=SourceSpan(start=span.start + part.start, end=span.start + part.end),
            )
        return MeetingPlan.model_validate(values)
