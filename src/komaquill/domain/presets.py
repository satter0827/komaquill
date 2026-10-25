"""Define pinned assets and generation presets independently of transport."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

ModelRole = Literal[
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
]
MODEL_ROLES: tuple[str, ...] = get_args(ModelRole)


class ModelPreset(BaseModel):
    """Pin one checkpoint, adapter or upscaler to verified upstream bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    role: ModelRole = "checkpoints"
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


class WorkflowBinding(BaseModel):
    """Bind one named scalar to matching standard API and UI inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str
    api_node: str
    api_input: str
    ui_node: int
    ui_widget: int = Field(ge=0)


class ConditioningRegion(BaseModel):
    """Use normalized rectangles independent of the selected image resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)
    strength: float = Field(default=1, ge=0, le=10)

    @model_validator(mode="after")
    def contained(self) -> "ConditioningRegion":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Conditioning regions must stay inside the image.")
        return self


class GenerationPreset(BaseModel):
    """Bind standard workflow files to explicit model and sampling settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    api: dict[str, Any]
    ui: dict[str, Any]
    bindings: tuple[WorkflowBinding, ...]
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
    quality_enabled: bool = True
    positive_prefix: str = ""
    negative_prefix: str = ""
    regions: tuple[ConditioningRegion, ...] = Field(default=(), max_length=2)
    single_region: ConditioningRegion = Field(default_factory=ConditioningRegion)
    sample_subjects: tuple[str, ...] = Field(default=(), max_length=2)
    negative: str = ""
    sample_prompt: str
    lora_strength: float = Field(default=1.0, ge=-10, le=10)
    upscale_width: int = Field(default=2048, ge=64, le=8192)
    upscale_height: int = Field(default=2048, ge=64, le=8192)

    @property
    def asset_ids(self) -> tuple[str, ...]:
        """Return only assets needed for this profile."""
        return tuple(value for value in (self.checkpoint, self.lora, self.upscaler) if value)

    @model_validator(mode="after")
    def validate_bindings(self) -> "GenerationPreset":
        """Reject incomplete or mismatched exports before GPU allocation."""
        nodes = {row.get("id"): row for row in self.ui.get("nodes", []) if isinstance(row, dict)}
        allowed = set(type(self).model_fields) - {
            "api",
            "ui",
            "bindings",
            "regions",
            "single_region",
            "sample_subjects",
        }
        allowed.update({"prompt", "seed", "output_prefix", "upscaled_output_prefix"})
        allowed.update({"subject_1", "subject_2", "prompt_delimiter"})
        allowed.update(
            f"region_{index}_{key}" for index in (1, 2) for key in ConditioningRegion.model_fields
        )
        if self.regions:
            regional = {"subject_1", "subject_2"} | {
                f"region_{index}_{key}"
                for index in (1, 2)
                for key in ConditioningRegion.model_fields
            }
            if len(self.regions) != 2 or not regional.issubset(
                {row.source for row in self.bindings}
            ):
                raise ValueError("Regional presets require two complete subject slots.")
        required = {"prompt", "negative", "seed", "checkpoint", "output_prefix"}
        if not required.issubset({row.source for row in self.bindings}):
            raise ValueError("The preset is missing required input bindings.")
        targets = set()
        for binding in self.bindings:
            node = self.api.get(binding.api_node, {})
            widget = nodes.get(binding.ui_node, {})
            inputs = node.get("inputs", {})
            values = widget.get("widgets_values", [])
            target = (binding.api_node, binding.api_input)
            if (
                binding.source not in allowed
                or target in targets
                or binding.api_input not in inputs
                or isinstance(inputs[binding.api_input], (dict, list))
                or node.get("class_type") != widget.get("type")
                or binding.ui_widget >= len(values)
            ):
                raise ValueError("A preset input does not match its standard workflow exports.")
            targets.add(target)
        return self
