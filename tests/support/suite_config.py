"""Freeze verification limits separately from product generation settings."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SuiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    seed: int = Field(ge=0, le=2**64 - 8)
    candidates: int = Field(ge=1)
    output_limit: int = Field(ge=1)
    improvement_limit: int = Field(ge=0)
    technical_retry_limit: int = Field(ge=0)
    send_seconds: int = Field(gt=0)
    total_seconds: int = Field(gt=0)
    storage_limit: int = Field(gt=0)
    planning_seconds: int = Field(gt=0)
    planning_calls: int = Field(gt=0)
    live_cases: tuple[str, ...]

    @model_validator(mode="after")
    def bounded(self):
        if self.send_seconds >= self.total_seconds or len(set(self.live_cases)) != len(
            self.live_cases
        ):
            raise ValueError("Verification limits and case identities are inconsistent.")
        return self


def defaults() -> SuiteConfig:
    path = Path(__file__).resolve().parents[1] / "fixtures/story-suite/suite.toml"
    return SuiteConfig.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
