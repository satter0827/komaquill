"""Identify safe response constraints without copying model or exception text."""

from typing import Literal


class ResponseConstraintError(ValueError):
    def __init__(self, rule: Literal["object_ownership", "empty_response"]) -> None:
        self.rule = rule
        super().__init__()

    @property
    def correction(self) -> str:
        if self.rule == "empty_response":
            return (
                "The companion is present. Answer the latest message using the retained facts, "
                "or describe an observable companion action. Do not return an empty reply."
            )
        return "Do not assign objects held by the player to the companion. Preserve ownership."
