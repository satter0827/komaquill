"""Compile portable image inputs without transport, storage or LLM dependencies."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.presets import GenerationPreset
from komaquill.domain.workflow import digest

MAX_SEED = 2**64 - 1
ImageSeed = Annotated[int, Field(strict=True, ge=0, le=MAX_SEED)]


def preset_fingerprint(profile: GenerationPreset, compiler_version: int = 2) -> str:
    """Preserve legacy fingerprints; canonicalize defaults for new snapshots."""
    return digest(profile.model_dump(mode="json", exclude_unset=compiler_version == 1))


class CompiledImageInput(BaseModel):
    """Retain original directions and the exact effective conditioning text."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    compiler_version: Literal[1, 2] = 1
    preset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str
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
    layout: Literal["global", "single", "left-right"]

    @model_validator(mode="after")
    def consistent(self) -> "CompiledImageInput":
        if (
            (
                self.compiler_version == 2
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
                joined(self.prompt, *self.subject_prompts)
                if self.layout == "global"
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


def compile_image_input(
    profile: GenerationPreset,
    prompt: str,
    negative: str | None = None,
    subjects: tuple[str, ...] = (),
) -> CompiledImageInput:
    """Apply model-specific quality once and freeze global or regional inputs."""
    positive_prefix = profile.positive_prefix if profile.quality_enabled else ""
    negative_prefix = profile.negative_prefix if profile.quality_enabled else ""
    body = prompt if profile.regions else joined(prompt, *subjects)
    return CompiledImageInput(
        compiler_version=2,
        preset_sha256=preset_fingerprint(profile),
        prompt=prompt,
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
        layout=("left-right" if len(subjects) == 2 else "single") if profile.regions else "global",
    )
