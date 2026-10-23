"""Define immutable runtime settings without reading the environment."""

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.presets import GenerationPreset, ModelPreset


class RuntimeSettings(BaseModel):
    """Keep the effective settings shared by one connection."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    poll_interval: float = Field(gt=0)
    request_timeout: float = Field(gt=0)
    generation_timeout: float = Field(gt=0)
    preparation_timeout: float = Field(gt=0)
    shutdown_timeout: float = Field(gt=0)
    execution_timeout: float = Field(gt=0)
    control_timeout: float = Field(gt=0)
    local_port: int = Field(ge=1024, le=65535)
    remote_port: int = Field(ge=1024, le=65535)
    gpu: str = Field(pattern=r"^(T4|L4|G4|H100|A100)$")

    profiles: tuple[str, ...] = ("pony",)
    presets: dict[str, GenerationPreset] = Field(default_factory=dict)
    assets: dict[str, ModelPreset] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_profiles(self) -> "RuntimeSettings":
        """Fail before allocation if a preset or required asset is undefined."""
        if not self.profiles or len(set(self.profiles)) != len(self.profiles):
            raise ValueError("Select at least one distinct generation profile.")
        for name in self.profiles:
            if name not in self.presets:
                raise ValueError("The selected generation profile is undefined.")
        for name, profile in self.presets.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise ValueError("Profile names must use lowercase letters, digits and hyphens.")
            for asset, role in (
                (profile.checkpoint, "checkpoints"),
                (profile.lora, "loras"),
                (profile.upscaler, "upscale_models"),
            ):
                if asset and (asset not in self.assets or self.assets[asset].role != role):
                    raise ValueError("A profile asset is missing or has the wrong role.")
        return self
