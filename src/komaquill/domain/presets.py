"""Define pinned assets and generation presets independently of transport."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelPreset(BaseModel):
    """Pin one checkpoint, adapter or upscaler to verified upstream bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    role: Literal[
        "checkpoints",
        "loras",
        "upscale_models",
        "diffusion_models",
        "text_encoders",
        "vae",
        "controlnet",
        "clip_vision",
        "embeddings",
        "style_models",
    ] = "checkpoints"
    repo_id: str = ""
    revision: str = ""
    url: str | None = None
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str
    license_url: str = ""
    local_only: bool = False
    size_bytes: int | None = Field(default=None, ge=0)

    @property
    def reference(self) -> str:
        """Return the unambiguous filename exposed by ComfyUI."""
        return f"{self.sha256}/{self.filename}"

    @model_validator(mode="after")
    def validate_source(self) -> "ModelPreset":
        """Reject ambiguous sources and paths outside the asset directory."""
        import re

        if (
            not self.filename
            or self.filename in {".", ".."}
            or PurePosixPath(self.filename).name != self.filename
            or PureWindowsPath(self.filename).name != self.filename
            or PureWindowsPath(self.filename).drive
        ):
            raise ValueError("Asset filenames must be plain filenames.")
        if self.local_only:
            if self.url or self.repo_id or self.revision:
                raise ValueError("Local model records must not contain download locations.")
            return self
        if self.url:
            from urllib.parse import parse_qs, urlsplit

            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or (
                    parsed.query
                    and (
                        parsed.hostname != "civitai.com"
                        or not set(parse_qs(parsed.query)).issubset(
                            {"type", "format", "size", "fp"}
                        )
                    )
                )
                or parsed.fragment
                or self.repo_id
                or self.revision
            ):
                raise ValueError("Use exactly one HTTPS asset source.")
        elif not self.repo_id or not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("Hugging Face sources require an immutable revision.")
        return self


class GenerationPreset(BaseModel):
    """Bind standard workflow files to explicit model and sampling settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    workflow: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    checkpoint: str
    lora: str | None = None
    upscaler: str | None = None
    width: int = Field(ge=64, le=4096, multiple_of=8)
    height: int = Field(ge=64, le=4096, multiple_of=8)
    steps: int = Field(ge=1, le=1000)
    cfg: float = Field(ge=0, le=100)
    sampler: str
    scheduler: str
    clip_skip: int = Field(default=-1, le=-1, ge=-24)
    positive_prefix: str = ""
    negative: str = ""
    sample_prompt: str
    lora_strength: float = Field(default=1.0, ge=-10, le=10)
    upscale_width: int = Field(default=2048, ge=64, le=8192)
    upscale_height: int = Field(default=2048, ge=64, le=8192)

    @property
    def asset_ids(self) -> tuple[str, ...]:
        """Return only assets needed for this profile."""
        return tuple(value for value in (self.checkpoint, self.lora, self.upscaler) if value)
