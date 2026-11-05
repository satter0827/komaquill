"""Validate optional HTTPS exposure without configuring authentication providers."""

import tomllib
from importlib.resources import files
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    origin: str | None = None
    max_body_bytes: int = Field(ge=1024, le=100 * 1024 * 1024)
    device_lifetime_days: int = Field(ge=1, le=365)
    pending_limit: int = Field(ge=1, le=128)
    websocket_seconds: int = Field(ge=1, le=600)

    @field_validator("origin")
    @classmethod
    def https_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
            raise ValueError("Configure one HTTPS origin without whitespace or a path.")
        parsed = HttpUrl(value)
        if (
            "@" in urlsplit(value).netloc
            or parsed.scheme != "https"
            or not parsed.host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query is not None
            or parsed.fragment is not None
        ):
            raise ValueError("Configure one HTTPS origin without credentials or a path.")
        return str(parsed).rstrip("/")


class WebOptions(WebSettings, BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KOMAQUILL_WEB__", extra="forbid", frozen=True)


def defaults() -> dict[str, object]:
    return tomllib.loads(
        files("komaquill").joinpath("resources/settings/web.toml").read_text("utf-8")
    )
