"""Describe cleanup candidates independently of filesystem operations."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CleanupItem:
    """Keep an explicit decision for every discovered working directory."""

    relative_path: str
    category: Literal["cache", "tmp", "dist"]
    size_bytes: int
    file_count: int
    reason: str | None = None
    result: str | None = None
