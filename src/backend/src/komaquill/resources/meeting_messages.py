"""Format recorded meeting answers for English transcript output."""

from komaquill.domain.conversations.meetings import MeetingAnswer
from komaquill.resources.messages import message


def meeting_text(answer: MeetingAnswer) -> str:
    if answer.status == "unknown":
        return message("chat.meeting_unknown")
    if answer.status == "cancelled":
        return message("chat.meeting_cancelled")
    unspecified = message("chat.meeting_unspecified")
    return message(
        "chat.meeting_confirmed",
        date=answer.date or unspecified,
        time=answer.time or unspecified,
        place=answer.place or unspecified,
    )
