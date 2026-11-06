"""Describe read-only availability checks using operation inputs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from komaquill.domain.errors import ErrorCode
from komaquill.domain.library.models import CreationRequest
from komaquill.domain.roleplay import Record


class AvailabilityRequest(Record):
    kind: Literal["conversation", "creation", "image", "collection", "resume"]
    identity: UUID | None = None
    creation: CreationRequest | None = None
    prompt: str = ""
    subjects: tuple[str, ...] = ()
    negative: str | None = None
    llm_profile: str = "fake"

    @model_validator(mode="after")
    def target_required(self) -> "AvailabilityRequest":
        if self.kind in {"conversation", "collection", "resume"} and self.identity is None:
            raise ValueError("An operation target is required.")
        if self.kind == "creation" and self.creation is None:
            raise ValueError("A creation request is required.")
        return self


class ConnectionObservation(Record):
    kind: Literal["llm", "comfy"]
    target: str
    state: Literal["ready", "unavailable", "unknown"]
    observed_at: datetime
    expires_at: datetime
    reason: ErrorCode | None = None


class Availability(Record):
    ready: bool
    reason: ErrorCode | None = None
    connections: list[ConnectionObservation] = Field(default_factory=list)
