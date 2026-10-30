"""Describe immutable input and explicitly saved workflow revisions."""

from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from komaquill.domain.errors import FailureInfo


class InputArtifact(BaseModel):
    """Reference immutable validated local bytes independently of a GPU endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1, 2, 3] = 2
    extension: Literal["png", "jpg", "webp"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    kind: Literal["image", "mask"] = "image"
    key: str = Field(pattern=r"^inputs/[a-f0-9]{64}/content$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    name: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @model_validator(mode="after")
    def portable(self) -> "InputArtifact":
        if (
            (self.format_version == 3) != (self.extension is not None)
            or self.key != f"inputs/{self.sha256}/content"
            or PurePosixPath(self.name).name != self.name
            or PureWindowsPath(self.name).name != self.name
            or PureWindowsPath(self.name).drive
            or self.name in {".", ".."}
            or ":" in self.name
            or any(ord(character) < 32 for character in self.name)
        ):
            raise ValueError("Use an immutable input key and a plain filename.")
        return self

    @property
    def reference(self) -> str:
        if self.format_version == 3:
            return f"komaquill-{self.sha256}.{self.extension}"
        if self.format_version == 1:
            return f"komaquill/{self.sha256}/{self.name}"
        return f"komaquill-{self.sha256}-{self.name}"


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
