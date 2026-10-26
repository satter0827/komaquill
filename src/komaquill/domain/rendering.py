"""Freeze per-scene production choices without introducing a workflow scheduler."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.materials import InputArtifact


class SceneRenderConfig(BaseModel):
    """Select a fixed preset and already imported materials for one scene."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    scene: int = Field(ge=1, strict=True)
    profile: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)
    prompt: str | None = None
    negative: str | None = None
    subjects: tuple[str, ...] | None = Field(default=None, max_length=2)
    parameters: dict[str, bool | int | float | str] = Field(default_factory=dict)


class RenderConfig(BaseModel):
    """Assign every scene before any image request is submitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: int = Field(default=2, ge=1, le=2, strict=True)
    common_prompt: str = ""
    scenes: tuple[SceneRenderConfig, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def distinct(self) -> "RenderConfig":
        if self.format_version == 1 and self.common_prompt:
            raise ValueError("Shared conditions require render configuration version 2.")
        if len({row.scene for row in self.scenes}) != len(self.scenes):
            raise ValueError("Each scene must have exactly one production configuration.")
        return self
