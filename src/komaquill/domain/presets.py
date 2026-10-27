"""Define pinned assets and generation presets independently of transport."""

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal, Self, get_args

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
    model_family: Literal["klein-4b", "klein-9b", "flux2", "illustrious"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    role: ModelRole = "checkpoints"
    repo_id: str = ""
    revision: str = ""
    url: str | None = None
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str
    license_url: str = ""
    local_only: bool = False
    transfer: bool = False
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
                            {"type", "format", "size", "fp", "fileId"}
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


class WorkflowImageSlot(BaseModel):
    """Declare one image or white-edit mask required by a fixed workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["image", "mask"] = "image"
    sample: str = ""
    match_base: bool = False


MODEL_INPUT_ROLES: dict[str, tuple[str, str]] = {
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoints"),
    "LoraLoader": ("lora_name", "loras"),
    "UNETLoader": ("unet_name", "diffusion_models"),
    "CLIPLoader": ("clip_name", "text_encoders"),
    "VAELoader": ("vae_name", "vae"),
    "LoraLoaderModelOnly": ("lora_name", "loras"),
}


class PresetSnapshot(BaseModel):
    """Bind standard workflow files to explicit model and sampling settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    api: dict[str, Any]
    ui: dict[str, Any]
    bindings: tuple[WorkflowBinding, ...]
    resources: dict[str, str] = Field(default_factory=dict)
    image_inputs: dict[str, WorkflowImageSlot] = Field(default_factory=dict)
    parameters: dict[str, bool | int | float | str] = Field(default_factory=dict)
    parameter_bounds: dict[str, tuple[float, float]] = Field(default_factory=dict)
    width: int = Field(ge=64, le=4096, multiple_of=16)
    height: int = Field(ge=64, le=4096, multiple_of=16)
    steps: int = Field(ge=1, le=1000)
    sampler: str
    scheduler: str
    quality_enabled: bool = True
    positive_prefix: str = ""
    batch_size: int = Field(default=1, ge=1, strict=True)
    primary_output_node: str | None = None
    sample_prompt: str

    @property
    def asset_ids(self) -> tuple[str, ...]:
        """Return only assets needed for this profile."""
        return tuple(dict.fromkeys(self.resources.values()))

    def configured_parameters(self, overrides: dict[str, bool | int | float | str]) -> Self:
        """Apply declared values without accepting type changes or unbounded numeric inputs."""
        allowed = set(self.parameters) | set(getattr(self, "adjustable", ()))
        if set(overrides) - allowed:
            raise ValueError("The workflow parameter is not declared.")
        standard = {}
        custom = dict(self.parameters)
        for name, value in overrides.items():
            expected = self.parameters[name] if name in self.parameters else getattr(self, name)
            if isinstance(expected, float):
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
                value = float(value) if valid else value
            else:
                valid = type(value) is type(expected)
            if not valid:
                raise ValueError("The workflow parameter has the wrong type.")
            if name in custom:
                custom[name] = value
            else:
                standard[name] = value
        return type(self).model_validate({**self.model_dump(), **standard, "parameters": custom})


class GenerationPreset(PresetSnapshot):
    """Validate positive-only executable workflows and their model family."""

    format_version: Literal[3] = 3
    model_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    operation: Literal["generate", "edit"]
    model_family: Literal["klein-4b", "klein-9b", "illustrious"]
    negative_default: str | None = None
    adjustable: tuple[str, ...] = ("width", "height")

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        """Reject incomplete or mismatched exports before GPU allocation."""
        nodes = {row.get("id"): row for row in self.ui.get("nodes", []) if isinstance(row, dict)}
        allowed = {
            "width",
            "height",
            "steps",
            "sampler",
            "scheduler",
            "batch_size",
            "positive_prefix",
            "prompt",
            "negative",
            "seed",
            "output_prefix",
            "prompt_delimiter",
        }
        declared = set(self.parameters) | {f"input_{name}" for name in self.image_inputs}
        if declared & allowed or set(self.resources) & (declared | allowed):
            raise ValueError("Workflow inputs must not shadow standard inputs.")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in declared | set(self.resources)
        ):
            raise ValueError("Use portable names for workflow inputs.")
        if set(self.adjustable) - {"width", "height", "steps", "sampler", "scheduler"}:
            raise ValueError("The adjustable standard parameter is unknown.")
        if set(self.parameter_bounds) - (set(self.parameters) | set(self.adjustable)):
            raise ValueError("Bounds must refer to declared parameters.")
        for name, (lower, upper) in self.parameter_bounds.items():
            value = self.parameters[name] if name in self.parameters else getattr(self, name)
            if (
                not isinstance(value, (float, int))
                or isinstance(value, bool)
                or not lower <= value <= upper
            ):
                raise ValueError("A workflow parameter is outside its declared bounds.")
        if (self.negative_default is not None) != (
            "negative" in {row.source for row in self.bindings}
        ):
            raise ValueError("Negative conditioning must match the declared graph input.")
        if self.model_family != "illustrious" and self.negative_default is not None:
            raise ValueError("Klein uses positive conditioning only.")
        allowed.update(self.resources)
        allowed.update(declared)
        if (self.primary_output_node is not None or self.batch_size != 1) and (
            self.api.get(self.primary_output_node or "", {}).get("class_type") != "SaveImage"
            or "batch_size" not in {row.source for row in self.bindings}
        ):
            raise ValueError("Candidate presets require a final image output and batch binding.")
        required = {"prompt", "seed", "batch_size", "output_prefix"}
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
