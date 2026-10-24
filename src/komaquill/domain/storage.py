"""Describe explicit workspace storage maintenance results."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MigrationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    files: int
    logical_bytes: int
    status: Literal["planned", "complete"] = "planned"
    destinations: list[str] = Field(default_factory=list)
