"""Read immutable image records produced by previous compiler versions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.materials import InputArtifact
from komaquill.domain.prompts import joined


class RetainedImageInput(BaseModel):
    """Retain original directions and the exact effective conditioning text."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1, 2, 3] = 1
    compiler_version: Literal[1, 2, 3, 4, 5, 6] = 1
    preset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str
    common_prompt: str = ""
    requested_negative: str | None
    subject_prompts: tuple[str, ...]
    quality_enabled: bool
    positive_prefix: str
    negative_prefix: str
    body: str
    positive: str
    negative: str
    negative_body: str
    subject_positives: tuple[str, ...]
    layout: Literal["global", "single", "left-right", "layered"]
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> "RetainedImageInput":
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


class RetainedPromptSource(BaseModel):
    """Retain original UTF-8 input without an absolute path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, pattern=r"^[^/\\]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str
    role: Literal["positive", "negative", "input"]


class RetainedPositiveImageInput(BaseModel):
    """Freeze the original directions and exact positive conditioning for generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[4] = 4
    compiler_version: Literal[7] = 7
    preset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str
    common_prompt: str = ""
    subject_prompts: tuple[str, ...]
    quality_enabled: bool
    positive_prefix: str
    body: str
    positive: str
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> "RetainedPositiveImageInput":
        if (
            any(not subject.strip() for subject in self.subject_prompts)
            or self.positive != joined(self.positive_prefix, self.body)
            or (not self.quality_enabled and self.positive_prefix)
            or self.body != joined(self.common_prompt, self.prompt, *self.subject_prompts)
        ):
            raise ValueError("The frozen image inputs are inconsistent.")
        return self
