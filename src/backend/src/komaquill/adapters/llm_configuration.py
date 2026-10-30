"""Validate explicit model endpoints without resolving credentials."""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["fake", "openai-compatible"]
    model: str = Field(min_length=1)
    base_url: str | None = None
    key_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
    credential_ref: str | None = None
    require_model_match: bool = False
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)

    @model_validator(mode="after")
    def endpoint(self) -> Self:
        if self.provider == "fake":
            if self.base_url or self.key_env or self.credential_ref:
                raise ValueError(
                    "Fake profiles must not contain connection or credential settings."
                )
            return self
        url = urlsplit(self.base_url or "")
        if (
            not url.hostname
            or url.port == 0
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.scheme not in {"http", "https"}
            or (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError("Use HTTPS or a loopback URL without credentials, query or fragment.")
        return self
