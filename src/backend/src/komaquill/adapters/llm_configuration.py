"""Validate explicit model endpoints without resolving credentials."""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    token_counting: Literal["approximate", "lmstudio"] = "approximate"
    schema_in_context: bool = True

    @model_validator(mode="after")
    def endpoint(self) -> Self:
        if self.provider == "fake":
            if (
                self.base_url
                or self.key_env
                or self.credential_ref
                or self.token_counting != "approximate"
            ):
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
        if self.token_counting == "lmstudio" and (
            url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or self.key_env
            or self.credential_ref
            or not self.require_model_match
        ):
            raise ValueError(
                "Native token counting requires a matched local model without credentials."
            )
        return self


class ConversationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    request_timeout: float = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    repair_attempts: int = Field(ge=0, le=1)
    recent_turns: int = Field(ge=1)
    max_input_characters: int = Field(gt=0)
    summary_max_output_tokens: int = Field(gt=0)
    summary_max_characters: int = Field(gt=0, le=2048)
    memory_max_batches: int = Field(ge=1, le=8)
    context_window_tokens: int = Field(gt=0)
    repair_reserve_tokens: int = Field(ge=0)
    llm_profiles: dict[str, LLMProfile]


class ConversationOptions(ConversationSettings, BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KOMAQUILL_CONVERSATION__",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )


class ProfileOptions(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KOMAQUILL_", env_nested_delimiter="__")
    llm_profiles: dict[str, LLMProfile] = {}
