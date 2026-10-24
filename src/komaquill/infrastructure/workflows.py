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
    values = profile.model_dump()
    values.update(
        prompt=prompt,
        negative=profile.negative if negative is None else negative,
        seed=seed,
        output_prefix=f"komaquill/{run_id}",
        upscaled_output_prefix=f"komaquill/{run_id}-upscaled",
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
