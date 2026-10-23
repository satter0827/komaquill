"""Describe immutable input and explicitly saved workflow revisions."""

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from komaquill.domain.errors import FailureInfo


class Material(BaseModel):
    """Track one observed file revision separately from its local acknowledgement."""

    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    material_id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    runtime_id: UUID | None = None
    kind: Literal["inputs", "workflows"]
    name: str
    reference: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    input_sha256: dict[str, str] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state: Literal["pending", "saved", "lost"] = "pending"
    failure: FailureInfo | None = None

    @field_validator("name", "reference")
    @classmethod
    def safe_reference(cls, value: str) -> str:
        """Keep server references relative without using them as local blob paths."""
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
            raise ValueError("Use a relative material reference.")
        return value
