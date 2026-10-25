"""Serialize reproducible planning conversations without SDK or storage dependencies."""

import json

from komaquill.application.stories.ports import planner_input
from komaquill.domain.errors import KomaquillError
from komaquill.domain.stories import PlannerProvenance, StoryInput


def chat_messages(
    request: StoryInput, provenance: PlannerProvenance, correction: str | None
) -> list[dict[str, str]]:
    """Keep the previous answer and actionable feedback in separate chat turns."""
    payload = planner_input(request, provenance, None)
    payload.pop("correction")
    messages = [
        {"role": "system", "content": provenance.instructions},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    if correction is not None:
        try:
            feedback = json.loads(correction)
        except ValueError:
            raise KomaquillError(code="invalid_output", stage="story_planning") from None
        if not isinstance(feedback, dict) or not isinstance(feedback.get("previous_output"), str):
            raise KomaquillError(code="invalid_output", stage="story_planning")
        previous = feedback.pop("previous_output")
        messages += [
            {"role": "assistant", "content": previous},
            {
                "role": "user",
                "content": json.dumps({"task": "repair", **feedback}, ensure_ascii=False),
            },
        ]
    return messages
