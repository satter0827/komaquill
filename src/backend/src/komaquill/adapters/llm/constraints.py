"""Identify safe response constraints without copying model or exception text."""

from typing import Literal


class ResponseConstraintError(ValueError):
    def __init__(
        self,
        rule: Literal["object_ownership", "empty_response", "companion_presence"],
        path: tuple[str | int, ...] = (),
    ) -> None:
        self.rule = rule
        self.path = path
        super().__init__()

    @property
    def correction(self) -> str:
        if self.rule == "companion_presence":
            return (
                "The companion remains at a different place but the reply includes their speech "
                "or visible action. Explicitly choose movement=join_player if the companion joins "
                "the player. Otherwise choose stay and leave text and action_narration empty. "
                "Keep the player's confirmed action and each person's facts unchanged."
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
