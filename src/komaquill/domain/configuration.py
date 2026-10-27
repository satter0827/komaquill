"""Define immutable runtime settings without reading the environment."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.presets import GenerationPreset, ModelPreset, PresetSnapshot
from komaquill.domain.retained_presets import RetainedGenerationPreset

GpuType = Literal["T4", "L4", "A100", "H100", "G4"]


def remaining_seconds(
    started_at: datetime | None, limit: float | None, now: datetime
) -> float | None:
    """Derive a durable time budget without resetting it after reconnection."""
    if started_at is None or limit is None:
        return None
    return max(0.0, limit - (now - started_at).total_seconds())


class SettingsSnapshot[PresetType: PresetSnapshot](BaseModel):
    """Keep the effective settings shared by one connection."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    max_candidates: int = Field(default=4, ge=1, strict=True)
    story_max_in_flight: int = Field(default=1, ge=1, strict=True)
    poll_interval: float = Field(gt=0)
    request_timeout: float = Field(gt=0)
    generation_timeout: float = Field(gt=0)
    preparation_timeout: float = Field(gt=0)
    shutdown_timeout: float = Field(gt=0)
    worker_execution_timeout: float = Field(gt=0)
    control_timeout: float = Field(gt=0)
    submission_timeout: float | None = Field(default=None, gt=0)
    session_timeout: float | None = Field(default=None, gt=0)
    local_port: int = Field(ge=1024, le=65535)
    remote_port: int = Field(ge=1024, le=65535)
    gpu: GpuType

    high_ram: bool = Field(default=False, strict=True)
    compute_unit_limit: float | None = Field(default=None, gt=0)
    compute_unit_reserve: float = Field(default=2, ge=0)

    profiles: tuple[str, ...]
    presets: dict[str, PresetType] = Field(default_factory=dict)
    assets: dict[str, ModelPreset] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_profiles(self) -> "SettingsSnapshot[PresetType]":
        """Fail before allocation if a preset or required asset is undefined."""
        if not self.profiles or len(set(self.profiles)) != len(self.profiles):
            raise ValueError("Select at least one distinct generation profile.")
        if self.submission_timeout is not None and (
            self.session_timeout is None or self.submission_timeout >= self.session_timeout
        ):
            raise ValueError("The submission deadline must precede the session deadline.")
        if (
            self.compute_unit_limit is not None
            and self.compute_unit_reserve >= self.compute_unit_limit
        ):
            raise ValueError("The compute-unit reserve must be smaller than the limit.")
        for name in self.profiles:
            if name not in self.presets:
                raise ValueError("The selected generation profile is undefined.")
            if self.presets[name].batch_size > self.max_candidates:
                raise ValueError("A selected preset exceeds the configured candidate limit.")
        for name, profile in self.presets.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise ValueError("Profile names must use lowercase letters, digits and hyphens.")
            if any(asset not in self.assets for asset in profile.resources.values()):
                raise ValueError("A profile resource is undefined.")
            if not isinstance(profile, GenerationPreset):
                continue
            from komaquill.domain.presets import MODEL_INPUT_ROLES

            for binding in profile.bindings:
                if binding.source not in profile.resources:
                    continue
                asset = self.assets[profile.resources[binding.source]]
                if (
                    isinstance(profile, GenerationPreset)
                    and asset.model_family is not None
                    and asset.model_family not in {profile.model_family, "flux2"}
                ):
                    raise ValueError("A profile mixes incompatible model families.")
                spec = MODEL_INPUT_ROLES.get(profile.api[binding.api_node]["class_type"])
                if spec is None or binding.api_input != spec[0] or asset.role != spec[1]:
                    raise ValueError("A profile resource does not match its standard model loader.")
        return self


class RuntimeSettings(SettingsSnapshot[GenerationPreset]):
    """Hold accepted settings for new generation requests."""


class RetainedRuntimeSettings(SettingsSnapshot[RetainedGenerationPreset]):
    """Read former session settings only for observation, collection and shutdown."""


RecordedSettings = RuntimeSettings | RetainedRuntimeSettings
