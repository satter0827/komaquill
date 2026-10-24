"""Read local planning configuration independently of GPU session settings."""

import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.configuration import Project


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


class StorySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KOMAQUILL_STORY_",
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        env_nested_delimiter="__",
    )
    images: int = Field(ge=1)
    max_images: int = Field(ge=1)
    llm_profile: str
    image_profile: str
    request_timeout: float = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    repair_attempts: int = Field(ge=0, le=1)
    llm_profiles: dict[str, LLMProfile]
    instructions_file: Path | None = None
    purpose: Literal["work", "verification"] = "work"
    case_id: str | None = None

    @model_validator(mode="after")
    def selected(self) -> Self:
        if self.llm_profile not in self.llm_profiles:
            raise ValueError("The image count or selected LLM profile is invalid.")
        return self


def settings(project: Project, **overrides: object) -> StorySettings:
    """Apply CLI, environment, workspace and packaged values in that order."""
    defaults = tomllib.loads(
        files("komaquill").joinpath("resources/stories/defaults.toml").read_text(encoding="utf-8")
    )
    workspace = tomllib.loads((project.root / "komaquill.toml").read_text(encoding="utf-8"))
    data = {**defaults, **workspace.get("story", {})}
    data["llm_profiles"] = {name: dict(value) for name, value in defaults["llm_profiles"].items()}
    for name, value in workspace.get("llm_profiles", {}).items():
        data["llm_profiles"][name] = {**data["llm_profiles"].get(name, {}), **value}

    def merge(base: dict[str, object], changes: dict[str, object]) -> dict[str, object]:
        result = dict(base)
        for key, value in changes.items():
            prior = result.get(key)
            result[key] = (
                merge(prior, value)
                if isinstance(prior, dict) and isinstance(value, dict)
                else value
            )
        return result

    combined = merge(data, EnvSettingsSource(StorySettings)())
    combined = merge(
        combined, {key: value for key, value in overrides.items() if value is not None}
    )
    return StorySettings.model_validate(combined)


def instructions(project: Project, settings: StorySettings) -> str:
    if settings.instructions_file is None:
        return (
            files("komaquill")
            .joinpath("resources/stories/instructions.md")
            .read_text(encoding="utf-8")
        )
    path = settings.instructions_file
    if not path.is_absolute():
        path = project.root / path
    text = path.read_bytes().decode("utf-8-sig")
    if not text.strip():
        raise KomaquillError(code="invalid_configuration", stage="story_planning")
    return text
