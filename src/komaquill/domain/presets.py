"""Define pinned assets and generation presets independently of transport."""

import re
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
    "ipadapter",
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


class WorkflowImageSlot(BaseModel):
    """Declare one image or white-edit mask required by a fixed workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["image", "mask"] = "image"
    sample: str = ""
    match_base: bool = False


class GenerationPreset(BaseModel):
    """Bind standard workflow files to explicit model and sampling settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    api: dict[str, Any]
    ui: dict[str, Any]
    bindings: tuple[WorkflowBinding, ...]
    resources: dict[str, str] = Field(default_factory=dict)
    extensions: tuple[Literal["ipadapter", "crop-stitch"], ...] = ()
    image_inputs: dict[str, WorkflowImageSlot] = Field(default_factory=dict)
    parameters: dict[str, bool | int | float | str] = Field(default_factory=dict)
    parameter_bounds: dict[str, tuple[float, float]] = Field(default_factory=dict)
    seed_offsets: dict[str, int] = Field(default_factory=dict)
    output_stages: dict[str, str] = Field(default_factory=dict)
    separate_subjects: bool = False
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
    batch_size: int = Field(default=1, ge=1, strict=True)
    primary_output_node: str | None = None
    negative: str = ""
    sample_prompt: str
    lora_strength: float = Field(default=1.0, ge=-10, le=10)
    upscale_width: int = Field(default=2048, ge=64, le=8192)
    upscale_height: int = Field(default=2048, ge=64, le=8192)

    @property
    def asset_ids(self) -> tuple[str, ...]:
        """Return only assets needed for this profile."""
        return tuple(
            dict.fromkeys(
                value
                for value in (self.checkpoint, self.lora, self.upscaler, *self.resources.values())
                if value
            )
        )

    def configured_parameters(
        self, overrides: dict[str, bool | int | float | str]
    ) -> "GenerationPreset":
        """Apply declared values without accepting type changes or unbounded numeric inputs."""
        if set(overrides) - set(self.parameters):
            raise ValueError("The workflow parameter is not declared.")
        for name, value in overrides.items():
            expected = self.parameters[name]
            if isinstance(expected, float):
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                valid = type(value) is type(expected)
            if not valid:
                raise ValueError("The workflow parameter has the wrong type.")
        return type(self).model_validate(
            {**self.model_dump(), "parameters": {**self.parameters, **overrides}}
        )

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
            "resources",
            "extensions",
            "image_inputs",
            "parameters",
            "parameter_bounds",
            "seed_offsets",
            "output_stages",
        }
        declared = set(self.parameters) | {f"input_{name}" for name in self.image_inputs}
        declared.update(f"seed_{name}" for name in self.seed_offsets)
        reserved = allowed | {
            "prompt",
            "seed",
            "output_prefix",
            "upscaled_output_prefix",
            "subject_1",
            "subject_2",
            "subject_1_body",
            "subject_2_body",
            "prompt_delimiter",
            "negative_delimiter",
        }
        if (declared & reserved) or (set(self.resources) & (declared | reserved)):
            raise ValueError("Workflow-specific inputs must not shadow standard inputs.")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in declared | set(self.resources)
        ):
            raise ValueError("Use portable names for workflow inputs.")
        if set(self.parameter_bounds) - set(self.parameters):
            raise ValueError("Bounds must refer to declared parameters.")
        for name, (lower, upper) in self.parameter_bounds.items():
            value = self.parameters[name]
            if (
                not isinstance(value, (float, int))
                or isinstance(value, bool)
                or not lower <= value <= upper
            ):
                raise ValueError("A workflow parameter is outside its declared bounds.")
        if any(
            self.api.get(node, {}).get("class_type") != "SaveImage" for node in self.output_stages
        ):
            raise ValueError("An intermediate output must refer to an image output node.")
        allowed.update(self.resources)
        allowed.update(declared)
        allowed.update({"prompt", "seed", "output_prefix", "upscaled_output_prefix"})
        allowed.update({"subject_1", "subject_2", "prompt_delimiter", "negative_delimiter"})
        allowed.update({"subject_1_body", "subject_2_body"})
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
        if (self.primary_output_node is not None or self.batch_size != 1) and (
            self.api.get(self.primary_output_node or "", {}).get("class_type") != "SaveImage"
            or "batch_size" not in {row.source for row in self.bindings}
        ):
            raise ValueError("Candidate presets require a final image output and batch binding.")
        required = {"prompt", "negative", "seed", "checkpoint", "output_prefix"}
        required.update(declared | set(self.resources))
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
