"""Bind preset parameters to standard ComfyUI workflow exports."""

import json
import tomllib
from importlib.resources import files
from typing import cast

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject, ModelPreset
from komaquill.domain.presets import GenerationPreset


def resource_json(name: str) -> JsonObject:
    """Read a packaged workflow without exposing arbitrary filesystem paths."""
    if "/" in name or "\\" in name or not name.endswith(".json"):
        raise KomaquillError(code="invalid_configuration")
    return cast(JsonObject, json.loads(files("komaquill").joinpath("resources", name).read_text()))


def preset(name: str = "pony") -> ModelPreset:
    """Read one pinned asset from the packaged catalog."""
    data = tomllib.loads(files("komaquill").joinpath("resources/presets.toml").read_text())
    return ModelPreset.model_validate(data["assets"][name])


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
) -> tuple[JsonObject, JsonObject]:
    """Apply identical conditions to API and native editable browser workflows."""
    if settings is None:
        defaults = tomllib.loads(files("komaquill").joinpath("resources/defaults.toml").read_text())
        catalog = tomllib.loads(files("komaquill").joinpath("resources/presets.toml").read_text())
        settings = RuntimeSettings.model_validate({**defaults, **catalog})
    _, profile = select(settings, profile_name)
    api = resource_json(profile.workflow + "-api.json")
    ui = resource_json(profile.workflow + ".json")
    # Node identities belong to the bundled workflow contract, not user configuration.
    values: dict[str, JsonObject] = {
        "3": {
            "seed": seed,
            "steps": profile.steps,
            "cfg": profile.cfg,
            "sampler_name": profile.sampler,
            "scheduler": profile.scheduler,
        },
        "4": {"ckpt_name": settings.assets[profile.checkpoint].filename},
        "5": {"width": profile.width, "height": profile.height},
        "7": {"text": profile.negative if negative is None else negative},
        "9": {"filename_prefix": f"komaquill/{run_id}"},
    }
    if "10" in api:
        values["10"] = {"string_a": profile.positive_prefix, "string_b": prompt}
        values["11"] = {"stop_at_clip_layer": profile.clip_skip}
    else:
        values["6"] = {"text": prompt}
    if profile.lora:
        values["12"] = {
            "lora_name": settings.assets[profile.lora].filename,
            "strength_model": profile.lora_strength,
        }
    if profile.upscaler:
        values["13"] = {"model_name": settings.assets[profile.upscaler].filename}
        values["15"] = {"width": profile.upscale_width, "height": profile.upscale_height}
        values["16"] = {"filename_prefix": f"komaquill/{run_id}-upscaled"}
    widgets: dict[str, list[str | None]] = {
        "3": ["seed", None, "steps", "cfg", "sampler_name", "scheduler", "denoise"],
        "4": ["ckpt_name"],
        "5": ["width", "height", "batch_size"],
        "6": ["text"],
        "7": ["text"],
        "9": ["filename_prefix"],
        "10": ["string_a", "string_b", "delimiter"],
        "11": ["stop_at_clip_layer"],
        "12": ["lora_name", "strength_model"],
        "13": ["model_name"],
        "15": ["upscale_method", "width", "height", "crop"],
        "16": ["filename_prefix"],
    }
    for key, changes in values.items():
        api[key]["inputs"].update(changes)
    for node in ui["nodes"]:
        key = str(node["id"])
        for index, name in enumerate(widgets.get(key, [])):
            if name in values.get(key, {}):
                node["widgets_values"][index] = values[key][name]
    return api, ui
