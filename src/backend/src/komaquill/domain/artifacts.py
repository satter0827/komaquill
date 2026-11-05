"""Identify immutable bytes independently of their physical placement."""

from pydantic import ConfigDict, Field

from komaquill.domain.roleplay import Record


class ObjectRef(Record):
    model_config = ConfigDict(frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
