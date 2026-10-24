"""Define validated runtime and persistence records."""

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import FailureInfo
from komaquill.domain.presets import ModelPreset as ModelPreset

JsonObject = dict[str, Any]


class Session(BaseModel):
    """Identify the endpoint and remote runtime without secrets."""

    endpoint: str
    runtime_id: UUID
    backend: Literal["colab", "external"] = "colab"
    source_revision: str | None = None
    code_sha256: str | None = None
    comfy_revision: str | None = None
    frontend_version: str | None = None
    model_sha256: str | None = None
    username: str | None = None
    settings: RuntimeSettings | None = None
    excluded_prompt_ids: set[str] = Field(default_factory=set)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str | None) -> str | None:
        """Reject empty usernames and Basic authentication delimiters."""
        if value is not None and (not value.strip() or any(char in value for char in ":\r\n")):
            raise ValueError("The Basic authentication username is invalid.")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """Allow TLS or loopback connections only."""
        url = urlsplit(value)
        # Accessing port triggers urlsplit numeric and range validation.
        if url.port == 0:
            raise ValueError("The endpoint port is invalid.")
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})
        ):
            raise ValueError("Use an HTTPS root URL without credentials, or an HTTP loopback URL.")
        return value.rstrip("/")


class ImageRef(BaseModel):
    """Reference an image reported by ComfyUI."""

    filename: str
    subfolder: str = ""
    type: Literal["output", "temp"] = "output"


class ImageMetadata(BaseModel):
    """Describe bytes that were verified and written to local storage."""

    sha256: str
    size_bytes: int
    width: int
    height: int
    source: ImageRef


class Run(BaseModel):
    """Retain generation conditions for collection after reconnecting."""

    format_version: int = 2
    run_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    runtime_id: UUID
    prompt_id: str | None = None
    state: Literal["prepared", "unknown", "queued", "saved", "failed", "lost"] = "prepared"
    app_version: str
    source_revision: str | None
    code_sha256: str | None = None
    remote_code_sha256: str | None = None
    remote_source_revision: str | None
    comfy_revision: str | None
    frontend_version: str | None
    model_sha256: str | None
    input_sha256: dict[str, str] = Field(default_factory=dict)
    asset_sha256: dict[str, str] = Field(default_factory=dict)
    workflow_sha256: str
    images: list[str] = Field(default_factory=list)
    error: str | None = None
    updated_at: datetime | None = None
    saved_at: datetime | None = None
    generation_started_at: datetime | None = None
    generation_completed_at: datetime | None = None
    image_metadata: dict[str, ImageMetadata] = Field(default_factory=dict)
    failure: FailureInfo | None = None

    @property
    def pending(self) -> bool:
        """Return whether the request still needs a terminal outcome."""
        return self.state not in {"saved", "failed", "lost"}


class RuntimeObservation(BaseModel):
    """Report provider evidence separately from local process and CLI cache state."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["running", "stopped", "unknown"] = "unknown"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    endpoint_id: str | None = None
    account_sha256: str | None = None

    @property
    def stopped(self) -> bool:
        """Require endpoint and account evidence before accepting termination."""
        return self.state == "stopped" and bool(self.endpoint_id and self.account_sha256)


class ShutdownResult(BaseModel):
    """Keep independent shutdown outcomes after ownership records are removed."""

    runtime_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    force: bool
    collection: str = "skipped"
    publications: str = "skipped"
    unpublished_ids: list[str] = Field(default_factory=list)
    materials: str = "skipped"
    pending_materials: list[str] = Field(default_factory=list)
    lost_materials: list[str] = Field(default_factory=list)
    logs: str = "skipped"
    gpu: str = "unknown"
    gpu_observation: RuntimeObservation | None = None
    connections: str = "ok"
    history: str = "skipped"
    pending_ids: list[UUID] = Field(default_factory=list)
    lost_ids: list[UUID] = Field(default_factory=list)
    errors: list[FailureInfo] = Field(default_factory=list)
