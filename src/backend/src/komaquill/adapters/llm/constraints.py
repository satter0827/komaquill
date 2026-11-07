"""Identify safe response constraints without copying model or exception text."""

from typing import Literal


class ResponseConstraintError(ValueError):
    def __init__(
        self,
        rule: Literal["object_ownership", "empty_response", "move_source", "meeting_source"],
        path: tuple[str | int, ...] = (),
    ) -> None:
        self.rule = rule
        self.path = path
        super().__init__()

    @property
    def correction(self) -> str:
        if self.rule == "move_source":
            return (
                "Quote the exact immediate travel clause once, excluding any other question "
                "or request. If the input does not explicitly request travel now, use null."
            )
        if self.rule == "meeting_source":
            return (
                "Quote one exact passage from the latest user message and copy each stated "
                "value verbatim. Use null for proposals, conditions or ambiguous decisions."
            )
        if self.rule == "empty_response":
            return (
                "The companion is present. Answer the latest message using the retained facts, "
                "or describe an observable companion action. Do not return an empty reply."
            )
        return (
            "The held_objects change assigns a human_player object to assistant_character. "
            "Remove that item from the companion's change. If the companion's possessions "
            "did not change, omit the entire held_objects change. Keep narration consistent "
            "with the separate owners."
        )
