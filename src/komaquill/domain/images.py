"""Compile portable image inputs without transport, storage or LLM dependencies."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.materials import InputArtifact
from komaquill.domain.presets import GenerationPreset
from komaquill.domain.prompts import joined
from komaquill.domain.retained_images import (
    RetainedImageInput,
    RetainedPositiveImageInput,
    RetainedPromptSource,
)
from komaquill.domain.workflow import digest

MAX_SEED = 2**64 - 1
ImageSeed = Annotated[int, Field(strict=True, ge=0, le=MAX_SEED)]


def preset_fingerprint(profile: GenerationPreset) -> str:
    """Hash expanded settings for current executable presets."""
    return digest(profile.model_dump(mode="json"))


class CompiledImageInput(BaseModel):
    """Freeze the original directions and exact positive conditioning for generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[5] = 5
    compiler_version: Literal[8] = 8
    preset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str
    requested_negative: str | None = None
    negative: str | None = None
    common_prompt: str = ""
    subject_prompts: tuple[str, ...]
    quality_enabled: bool
    positive_prefix: str
    body: str
    positive: str
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> "CompiledImageInput":
        if (
            (self.requested_negative is not None and self.negative != self.requested_negative)
            or any(not subject.strip() for subject in self.subject_prompts)
            or self.positive != joined(self.positive_prefix, self.body)
            or (not self.quality_enabled and self.positive_prefix)
            or self.body != joined(self.common_prompt, self.prompt, *self.subject_prompts)
        ):
            raise ValueError("The frozen image inputs are inconsistent.")
        return self

    def matches(self, profile: GenerationPreset) -> bool:
        """Check frozen input without applying directions again."""
        return (
            self.negative
            == (
                profile.negative_default
                if self.requested_negative is None
                else self.requested_negative
            )
            and self.preset_sha256 == preset_fingerprint(profile)
            and self.quality_enabled == profile.quality_enabled
            and self.positive_prefix == (profile.positive_prefix if profile.quality_enabled else "")
        )


CompiledInputRecord = CompiledImageInput | RetainedPositiveImageInput | RetainedImageInput


def validate_inputs(profile: GenerationPreset, inputs: dict[str, InputArtifact]) -> None:
    """Reject missing, unexpected or dimensionally incompatible workflow inputs."""
    if set(inputs) != set(profile.image_inputs) or any(
        inputs[name].kind != slot.kind for name, slot in profile.image_inputs.items()
    ):
        raise ValueError("Supply every declared image and mask input.")
    base = inputs.get("base")
    if base is not None and any(
        (item.width, item.height) != (base.width, base.height)
        for name, item in inputs.items()
        if item.kind == "mask" or profile.image_inputs[name].match_base
    ):
        raise ValueError("Canvas images and masks must have the base image dimensions.")
    if base is None and any(slot.match_base for slot in profile.image_inputs.values()):
        raise ValueError("Canvas-relative inputs require a base image.")


def compile_image_input(
    profile: GenerationPreset,
    prompt: str,
    subjects: tuple[str, ...] = (),
    inputs: dict[str, InputArtifact] | None = None,
    *,
    common_prompt: str = "",
    negative: str | None = None,
) -> CompiledImageInput:
    """Apply model-specific quality once and freeze positive directions."""
    if negative is not None and profile.negative_default is None:
        raise ValueError("This model does not support negative conditioning.")
    effective_negative = profile.negative_default if negative is None else negative
    positive_prefix = profile.positive_prefix if profile.quality_enabled else ""
    body = joined(common_prompt, prompt, *subjects)
    return CompiledImageInput(
        inputs=inputs or {},
        requested_negative=negative,
        negative=effective_negative,
        preset_sha256=preset_fingerprint(profile),
        prompt=prompt,
        common_prompt=common_prompt,
        subject_prompts=subjects,
        quality_enabled=profile.quality_enabled,
        positive_prefix=positive_prefix,
        body=body,
        positive=joined(positive_prefix, body),
    )


class PromptSource(BaseModel):
    """Retain original UTF-8 input without an absolute path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, pattern=r"^[^/\\]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str
    role: Literal["positive", "negative", "input"]


PromptSourceRecord = PromptSource | RetainedPromptSource


class ImageInput(BaseModel):
    """Validate positive and optional subject directions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: Literal[4] = 4
    negative: str | None = None
    model: str | None = None
    operation: Literal["generate", "edit"] = "generate"
    prompt: str = Field(min_length=1)
    subjects: tuple[str, ...] = ()
    inputs: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, bool | int | float | str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def nonempty(self) -> "ImageInput":
        if not self.prompt.strip() or any(not value.strip() for value in self.subjects):
            raise ValueError("Image descriptions must not be empty.")
        return self
