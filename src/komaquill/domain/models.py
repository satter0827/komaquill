"""Define validated runtime and persistence records."""

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import FailureInfo

JsonObject = dict[str, Any]


class ModelPreset(BaseModel):
    """Pin a model to an immutable upstream revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    repo_id: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str


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


class ShutdownResult(BaseModel):
    """Keep independent shutdown outcomes after ownership records are removed."""

    runtime_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    force: bool
    collection: str = "skipped"
    logs: str = "skipped"
    gpu: str = "unknown"
    connections: str = "ok"
    history: str = "skipped"
    pending_ids: list[UUID] = Field(default_factory=list)
    lost_ids: list[UUID] = Field(default_factory=list)
    errors: list[FailureInfo] = Field(default_factory=list)
