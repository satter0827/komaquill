"""Describe one locally supervised session independently of its provider."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import FailureInfo


class MonitorState(BaseModel):
    """Persist a process identity and the last observed operation phase."""

    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    operation_id: UUID
    backend: Literal["colab", "external"]
    settings: RuntimeSettings
    endpoint: str | None = None
    username: str | None = None
    open_browser: bool = True
    pid: int = 0
    process_started_at: float = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase_started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: str = "starting"
    asset: str | None = None
    runtime_id: UUID | None = None
    failure: FailureInfo | None = None
    browser_opened: bool | None = None
