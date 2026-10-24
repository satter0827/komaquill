"""Describe append-only human and AI evaluations independently of execution."""

import hashlib
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Evaluator(BaseModel):
    """Identify the reviewer and the instructions used for AI evaluation."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["human", "ai"] = "human"
    name: str = Field(default="user", min_length=1)
    model: str | None = None
    model_version: str | None = None
    instruction: str | None = None
    instruction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def provenance(self) -> Self:
        """Require AI attribution and reject mismatched instruction snapshots."""
        if self.instruction is not None:
            actual = hashlib.sha256(self.instruction.encode()).hexdigest()
            if self.instruction_sha256 not in {None, actual}:
                raise ValueError("Instruction content does not match its checksum.")
            self.instruction_sha256 = actual
        if self.kind == "ai" and (not self.model or not self.instruction_sha256):
            raise ValueError("AI evaluation requires a model and instruction checksum.")
        return self


class Evaluation(BaseModel):
    """Keep adoption decisions separate from pairwise preference."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["decision"],
                    "properties": {
                        "comparison_run_id": {"type": "null"},
                        "preference": {"type": "null"},
                        "decision": {"enum": ["accept", "reject", "undecided"]},
                    },
                },
                {
                    "required": ["comparison_run_id", "preference"],
                    "properties": {
                        "comparison_run_id": {"type": "string", "format": "uuid"},
                        "decision": {"type": "null"},
                        "preference": {"enum": ["first", "second", "equal", "undecided"]},
                    },
                },
            ]
        },
    )
    format_version: Literal[1] = 1
    evaluation_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: UUID
    comparison_run_id: UUID | None = None
    image: str | None = None
    comparison_image: str | None = None
    criterion: str = Field(min_length=1)
    criterion_version: str = Field(default="1", min_length=1)
    reason: str = Field(min_length=1)
    evaluator: Evaluator = Field(default_factory=Evaluator)
    decision: Literal["accept", "reject", "undecided"] | None = None
    preference: Literal["first", "second", "equal", "undecided"] | None = None
    case_id: str | None = None
    supersedes: UUID | None = None
    workflow_sha256: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        """Reject ambiguous imported timestamps before sorting records."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Evaluation timestamps require a timezone.")
        return value

    @model_validator(mode="after")
    def comparison(self) -> Self:
        """Require one unambiguous evaluation mode."""
        if self.comparison_run_id is None:
            if self.decision is None or self.preference is not None or self.comparison_image:
                raise ValueError("A single-run evaluation requires an adoption decision.")
        elif (
            self.comparison_run_id == self.run_id
            or self.preference is None
            or self.decision is not None
        ):
            raise ValueError("A comparison requires two distinct runs and a preference.")
        if self.supersedes == self.evaluation_id:
            raise ValueError("An evaluation cannot supersede itself.")
        return self
