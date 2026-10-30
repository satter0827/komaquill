"""Read settings recorded before positive-only Klein profiles were introduced."""

from typing import Literal

from pydantic import Field

from komaquill.domain.presets import PresetSnapshot


class RetainedGenerationPreset(PresetSnapshot):
    """Keep former fields without applying current generation acceptance rules."""

    format_version: Literal[1] = Field(default=1, exclude=True)
    cfg: float = Field(ge=0, le=100)
    negative_prefix: str = ""
    negative: str = ""


class RetainedPositivePreset(PresetSnapshot):
    """Read positive-only Klein snapshots without adding current input defaults."""

    format_version: Literal[2] = 2
    model_family: Literal["klein-4b", "klein-9b"] = "klein-4b"
