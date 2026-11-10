"""Bind preset parameters to standard ComfyUI workflow exports."""

import json
import tomllib
from importlib.resources import files
from typing import cast

from komaquill.domain.configuration import RecordedSettings, RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import CompiledImageInput, compile_image_input
from komaquill.domain.models import JsonObject, ModelPreset
from komaquill.domain.presets import GenerationPreset
from komaquill.domain.prompts import joined
from komaquill.resources.messages import message

_INPUT_LABELS = {
    "prompt": "workflow.prompt",
    "negative": "workflow.negative",
    "positive_prefix": "workflow.quality_positive",
    "common_prompt": "workflow.common_positive",
    "width": "workflow.width",
    "height": "workflow.height",
    "seed": "workflow.seed",
    "steps": "workflow.steps",
    "cfg": "workflow.cfg",
    "sampler": "workflow.sampler",
    "scheduler": "workflow.scheduler",
    "aniedit_strength": "workflow.aniedit_strength",
    "detail_strength": "workflow.detail_strength",
    "reference_megapixels": "workflow.reference_size",
    "megapixels": "workflow.image_size",
    "change_strength": "workflow.change_strength",
    "batch_size": "workflow.candidates",
    "input_reference": "workflow.reference",
}


def resource_json(name: str) -> JsonObject:
    """Read a packaged workflow without exposing arbitrary filesystem paths."""
    from pathlib import PurePosixPath

    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not name.endswith(".json"):
        raise KomaquillError(code="invalid_configuration")
    return cast(
        JsonObject, json.loads(files("komaquill").joinpath("resources", *path.parts).read_text())
    )


def select(settings: RecordedSettings, name: str | None = None) -> tuple[str, GenerationPreset]:
    """Resolve only profiles prepared for the current connection."""
    if not isinstance(settings, RuntimeSettings):
        raise KomaquillError(code="invalid_configuration", stage="generation", retryable=False)
    selected = name or settings.profiles[0]
    if selected not in settings.profiles:
        raise KomaquillError(
            display_key="profile.not_enabled",
            details={"profile": selected},
            code="invalid_request",
            retryable=False,
        )
    return selected, settings.presets[selected]


def assets(settings: RecordedSettings) -> tuple[ModelPreset, ...]:
    """Deduplicate shared weights across the selected profiles."""
    names = dict.fromkeys(
        asset for name in settings.profiles for asset in settings.presets[name].asset_ids
    )
    return tuple(settings.assets[name] for name in names)


def prepare(
    prompt: str,
    seed: int,
    run_id: str,
    *,
    settings: RecordedSettings | None = None,
    profile_name: str | None = None,
    compiled_input: CompiledImageInput | None = None,
    subject_prompts: tuple[str, ...] = (),
    randomize_seed: bool = False,
) -> tuple[JsonObject, JsonObject]:
    """Apply identical conditions to API and native editable browser workflows."""
    if settings is None:
        defaults = tomllib.loads(
            files("komaquill").joinpath("resources/settings/defaults.toml").read_text()
        )
        from komaquill.adapters.preset_catalog import catalog

        settings = RuntimeSettings.model_validate({**defaults, **catalog()})
    _, profile = select(settings, profile_name)
    from copy import deepcopy

    api = deepcopy(profile.api)
    ui = deepcopy(profile.ui)
    applied = compiled_input or compile_image_input(profile, prompt, subject_prompts)
    if not isinstance(applied, CompiledImageInput) or not applied.matches(profile):
        raise KomaquillError(code="invalid_configuration", stage="generation")
    values = profile.model_dump()
    separate_common = any(binding.source == "common_prompt" for binding in profile.bindings)
    values.update(
        prompt=joined(applied.prompt, *applied.subject_prompts)
        if separate_common
        else applied.body,
        negative=applied.negative,
        positive_prefix=applied.positive_prefix,
        common_prompt=applied.common_prompt,
        common_delimiter=", " if applied.common_prompt else "",
        prompt_delimiter=", " if applied.positive_prefix and applied.body else "",
        seed=seed,
        output_prefix=f"komaquill/{run_id}",
    )
    values.update(
        {name: settings.assets[asset].reference for name, asset in profile.resources.items()}
    )
    values.update(profile.parameters)
    values.update({f"input_{name}": slot.sample for name, slot in profile.image_inputs.items()})
    values.update({f"input_{name}": item.reference for name, item in applied.inputs.items()})
    nodes = {row["id"]: row for row in ui["nodes"]}
    for binding in profile.bindings:
        value = values[binding.source]
        api[binding.api_node]["inputs"][binding.api_input] = value
        nodes[binding.ui_node]["widgets_values"][binding.ui_widget] = value
        if randomize_seed and binding.source == "seed":
            widgets = nodes[binding.ui_node]["widgets_values"]
            control = binding.ui_widget + 1
            if control < len(widgets) and widgets[control] == "fixed":
                widgets[control] = "randomize"
        if binding.source in _INPUT_LABELS:
            for slot in nodes[binding.ui_node].get("inputs", []):
                if slot.get("name") == binding.api_input:
                    slot["label"] = message(
                        "workflow.edit_instructions"
                        if binding.source == "prompt"
                        and profile.operation == "edit"
                        and profile.model_family.startswith("klein-")
                        else _INPUT_LABELS[binding.source]
                    )
    for subgraph in ui.get("definitions", {}).get("subgraphs", []):
        for node in subgraph.get("nodes", []):
            properties = node.get("properties", {})
            if asset_id := properties.pop("komaquill_asset", None):
                asset = settings.assets[asset_id]
                node["widgets_values"][0] = asset.reference
                url = (
                    asset.url
                    or f"https://huggingface.co/{asset.repo_id}/resolve/{asset.revision}/{asset.filename}"
                )
                properties["models"] = [
                    {"name": asset.reference, "directory": asset.role, "url": url}
                ]
            if properties.pop("komaquill_sampling_defaults", False):
                node["widgets_values"][:6] = [
                    seed,
                    "randomize" if randomize_seed else "fixed",
                    profile.steps,
                    profile.parameters["cfg"],
                    profile.sampler,
                    profile.scheduler,
                ]
    return api, ui
