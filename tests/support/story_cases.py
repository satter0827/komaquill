"""Load paired sources, fake plans and independent story acceptance criteria."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from komaquill.domain.stories import StoryDraft

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/story-suite/cases.json"


class StoryCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z]+(?:-[a-z]+)*$")
    group: Literal["development", "confirmation"]
    source: str
    required: list[str] = Field(min_length=1)
    forbidden: list[str] = Field(min_length=1)
    visual_checks: list[str] = Field(min_length=1)
    draft: StoryDraft

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()


def cases() -> dict[str, StoryCase]:
    values = [StoryCase.model_validate(row) for row in json.loads(CORPUS.read_bytes())]
    if len({row.id for row in values}) != len(values):
        raise ValueError("Story case identifiers must be distinct.")
    return {row.id: row for row in values}
