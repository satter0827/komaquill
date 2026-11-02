"""Identify safe response constraints without copying model or exception text."""

from typing import Literal


class ResponseConstraintError(ValueError):
    def __init__(self, rule: Literal["object_ownership"]) -> None:
        self.rule = rule
        super().__init__()

    @property
    def correction(self) -> str:
        return "Do not assign objects held by the player to the companion. Preserve ownership."
