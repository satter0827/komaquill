"""Compile portable image inputs without transport, storage or LLM dependencies."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.materials import InputArtifact
from komaquill.domain.presets import GenerationPreset
from komaquill.domain.workflow import digest

MAX_SEED = 2**64 - 1
ImageSeed = Annotated[int, Field(strict=True, ge=0, le=MAX_SEED)]


def preset_fingerprint(profile: GenerationPreset, compiler_version: int = 5) -> str:
    """Preserve legacy fingerprints; canonicalize defaults for new snapshots."""
    value = profile.model_dump(
        mode="json",
        exclude_unset=compiler_version == 1,
        exclude=(
            ({"batch_size", "primary_output_node"} if compiler_version < 3 else set())
            | (
                {
                    "resources",
                    "extensions",
                    "image_inputs",
                    "parameters",
                    "parameter_bounds",
                    "seed_offsets",
                    "output_stages",
                    "separate_subjects",
                }
                if compiler_version < 4
                else set()
            )
        ),
    )
    if compiler_version < 5:
        for slot in value.get("image_inputs", {}).values():
            slot.pop("match_base", None)
    return digest(value)


class CompiledImageInput(BaseModel):
    """Retain original directions and the exact effective conditioning text."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1, 2, 3] = 1
    compiler_version: Literal[1, 2, 3, 4, 5] = 1
    preset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str
    common_prompt: str = ""
    requested_negative: str | None
    subject_prompts: tuple[str, ...] = Field(max_length=2)
    quality_enabled: bool
    positive_prefix: str
    negative_prefix: str
    body: str
    positive: str
    negative: str
    negative_body: str
    subject_positives: tuple[str, ...] = Field(max_length=2)
    layout: Literal["global", "single", "left-right", "layered"]
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> "CompiledImageInput":
        if (
            (self.compiler_version < 5 and bool(self.common_prompt))
            or (
                self.compiler_version >= 2
                and (
                    any(not subject.strip() for subject in self.subject_prompts)
                    or (
                        self.requested_negative is not None
                        and self.negative_body != self.requested_negative
                    )
                )
            )
            or self.positive != joined(self.positive_prefix, self.body)
            or self.negative != joined(self.negative_prefix, self.negative_body)
            or self.subject_positives
            != tuple(joined(self.positive_prefix, text) for text in self.subject_prompts)
            or (not self.quality_enabled and (self.positive_prefix or self.negative_prefix))
            or self.body
            != (
                joined(self.common_prompt, self.prompt, *self.subject_prompts)
                if self.layout == "global"
                else joined(self.common_prompt, self.prompt)
                if self.compiler_version >= 5
                else self.prompt
            )
            or (self.layout == "left-right" and len(self.subject_prompts) != 2)
            or (self.layout == "single" and len(self.subject_prompts) > 1)
        ):
            raise ValueError("The retained image inputs are inconsistent.")
        return self

    def matches(self, profile: GenerationPreset) -> bool:
        """Check a frozen input against its frozen preset without reapplying text."""
        if self.compiler_version == 1:
            return self.preset_sha256 == preset_fingerprint(profile, 1)
        layout = (
            ("left-right" if len(self.subject_prompts) == 2 else "single")
            if profile.regions
            else "layered"
            if profile.separate_subjects
            else "global"
        )
        return (
            self.preset_sha256 == preset_fingerprint(profile, self.compiler_version)
            and self.quality_enabled == profile.quality_enabled
            and self.positive_prefix == (profile.positive_prefix if profile.quality_enabled else "")
            and self.negative_prefix == (profile.negative_prefix if profile.quality_enabled else "")
            and self.negative_body
            == (profile.negative if self.requested_negative is None else self.requested_negative)
            and self.layout == layout
        )


def joined(*parts: str) -> str:
    return ", ".join(part.strip() for part in parts if part.strip())


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
    negative: str | None = None,
    subjects: tuple[str, ...] = (),
    inputs: dict[str, InputArtifact] | None = None,
    *,
    common_prompt: str = "",
) -> CompiledImageInput:
    """Apply model-specific quality once and freeze global or regional inputs."""
    positive_prefix = profile.positive_prefix if profile.quality_enabled else ""
    negative_prefix = profile.negative_prefix if profile.quality_enabled else ""
    context = joined(common_prompt, prompt)
    body = context if profile.regions or profile.separate_subjects else joined(context, *subjects)
    return CompiledImageInput(
        format_version=3,
        compiler_version=5,
        inputs=inputs or {},
        preset_sha256=preset_fingerprint(profile),
        prompt=prompt,
        common_prompt=common_prompt,
        requested_negative=negative,
        subject_prompts=subjects,
        quality_enabled=profile.quality_enabled,
        positive_prefix=positive_prefix,
        negative_prefix=negative_prefix,
        body=body,
        positive=joined(positive_prefix, body),
        negative=joined(negative_prefix, profile.negative if negative is None else negative),
        negative_body=profile.negative if negative is None else negative,
        subject_positives=tuple(joined(positive_prefix, subject) for subject in subjects),
        layout=("left-right" if len(subjects) == 2 else "single")
        if profile.regions
        else "layered"
        if profile.separate_subjects
        else "global",
    )


class PromptSource(BaseModel):
    """Retain original UTF-8 input without an absolute path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, pattern=r"^[^/\\]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str
    role: Literal["positive", "negative", "input"]


class ImageInput(BaseModel):
    """Validate positive, negative and optional regional directions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: Literal[1, 2] = 1
    prompt: str = Field(min_length=1)
    negative: str | None = None
    subjects: tuple[str, ...] = Field(default=(), max_length=2)
    inputs: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, bool | int | float | str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def nonempty(self) -> "ImageInput":
        if not self.prompt.strip() or any(not value.strip() for value in self.subjects):
            raise ValueError("Image descriptions must not be empty.")
        return self
