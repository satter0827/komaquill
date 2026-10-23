"""Apply generation conditions to the fixed workflow."""

import copy
import json
import tomllib
from importlib.resources import files
from typing import cast

from komaquill.domain.models import JsonObject, ModelPreset


def resource_json(name: str) -> JsonObject:
    """Read a packaged workflow."""
    return cast(JsonObject, json.loads(files("komaquill").joinpath("resources", name).read_text()))


def preset() -> ModelPreset:
    """Read the pinned model definition."""
    data = tomllib.loads(files("komaquill").joinpath("resources", "model.toml").read_text())
    return ModelPreset.model_validate(data)


def prepare(prompt: str, negative: str, seed: int, run_id: str) -> tuple[JsonObject, JsonObject]:
    """Apply identical conditions to API and browser workflows."""
    api = copy.deepcopy(resource_json("workflow-api.json"))
    ui = copy.deepcopy(resource_json("workflow.json"))
    api["6"]["inputs"]["text"] = prompt
    api["7"]["inputs"]["text"] = negative
    api["3"]["inputs"]["seed"] = seed
    api["9"]["inputs"]["filename_prefix"] = f"komaquill/{run_id}"
    replacements = {6: [prompt], 7: [negative], 9: [f"komaquill/{run_id}"]}
    for node in ui["nodes"]:
        if node["id"] in replacements:
            node["widgets_values"] = replacements[node["id"]]
        elif node["id"] == 3:
            node["widgets_values"][0] = seed
    return api, ui
