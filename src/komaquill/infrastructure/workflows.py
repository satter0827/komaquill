"""Bind preset parameters to standard ComfyUI workflow exports."""

import json
import tomllib
from importlib.resources import files
from typing import cast

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import CompiledImageInput, compile_image_input
from komaquill.domain.models import JsonObject, ModelPreset
from komaquill.domain.presets import GenerationPreset


def resource_json(name: str) -> JsonObject:
    """Read a packaged workflow without exposing arbitrary filesystem paths."""
    from pathlib import PurePosixPath

    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not name.endswith(".json"):
        raise KomaquillError(code="invalid_configuration")
    return cast(
        JsonObject, json.loads(files("komaquill").joinpath("resources", *path.parts).read_text())
    )


def preset(name: str = "pony") -> ModelPreset:
    """Read one pinned asset from the packaged catalog."""
    from komaquill.infrastructure.preset_catalog import catalog

    return ModelPreset.model_validate(catalog()["assets"][name])


def select(settings: RuntimeSettings, name: str | None = None) -> tuple[str, GenerationPreset]:
    """Resolve only profiles prepared for the current connection."""
    selected = name or settings.profiles[0]
    if selected not in settings.profiles:
        raise KomaquillError(
            display_key="profile.not_enabled",
            details={"profile": selected},
            code="invalid_request",
            retryable=False,
        )
    return selected, settings.presets[selected]


def assets(settings: RuntimeSettings) -> tuple[ModelPreset, ...]:
    """Deduplicate shared weights across the selected profiles."""
    names = dict.fromkeys(
        asset for name in settings.profiles for asset in settings.presets[name].asset_ids
    )
    return tuple(settings.assets[name] for name in names)


def prepare(
    prompt: str,
    negative: str | None,
    seed: int,
    run_id: str,
    *,
    settings: RuntimeSettings | None = None,
    profile_name: str | None = None,
    compiled_input: CompiledImageInput | None = None,
    subject_prompts: tuple[str, ...] = (),
) -> tuple[JsonObject, JsonObject]:
    """Apply identical conditions to API and native editable browser workflows."""
    if settings is None:
        defaults = tomllib.loads(
            files("komaquill").joinpath("resources/settings/defaults.toml").read_text()
        )
        from komaquill.infrastructure.preset_catalog import catalog

        settings = RuntimeSettings.model_validate({**defaults, **catalog()})
    _, profile = select(settings, profile_name)
    from copy import deepcopy

    api = deepcopy(profile.api)
    ui = deepcopy(profile.ui)
    applied = compiled_input or compile_image_input(profile, prompt, negative, subject_prompts)
    if not applied.matches(profile):
        raise KomaquillError(code="invalid_configuration", stage="generation")
    sources = {row.source for row in profile.bindings}
    negative_cleaned = any(
        node.get("class_type") == "RegexReplace"
        and node.get("inputs", {}).get("string") == [row.api_node, 0]
        for row in profile.bindings
        if row.source == "negative"
        for node in profile.api.values()
    )
    values = profile.model_dump()
    values.update(
        prompt=applied.body if "positive_prefix" in sources else applied.positive,
        positive_prefix=applied.positive_prefix,
        prompt_delimiter=", " if applied.positive_prefix and applied.body else "",
        negative=applied.negative_body if "negative_prefix" in sources else applied.negative,
        negative_prefix=applied.negative_prefix,
        negative_delimiter=", "
        if negative_cleaned or (applied.negative_prefix and applied.negative_body)
        else "",
        seed=seed,
        output_prefix=f"komaquill/{run_id}",
        upscaled_output_prefix=f"komaquill/{run_id}-upscaled",
    )
    for index in (1, 2):
        values[f"subject_{index}"] = (
            applied.subject_positives[index - 1] if index <= len(applied.subject_positives) else ""
        )
    for index, rectangle in enumerate(profile.regions, 1):
        region = profile.single_region if applied.layout == "single" else rectangle
        for key, value in region.model_dump().items():
            values[f"region_{index}_{key}"] = (
                0.0 if key == "strength" and index > len(applied.subject_positives) else value
            )
    for name in ("checkpoint", "lora", "upscaler"):
        asset = getattr(profile, name)
        if asset is not None:
            values[name] = settings.assets[asset].reference
    nodes = {row["id"]: row for row in ui["nodes"]}
    for binding in profile.bindings:
        value = values[binding.source]
        api[binding.api_node]["inputs"][binding.api_input] = value
        nodes[binding.ui_node]["widgets_values"][binding.ui_widget] = value
    return api, ui
