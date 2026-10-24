"""Load complete preset folders and shared immutable model declarations."""

import json
import re
import tomllib
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import GenerationPreset


def bundle(folder: Traversable) -> dict[str, Any]:
    """Require one complete set without mixing local and packaged files."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", folder.name):
        raise KomaquillError(code="invalid_configuration")
    required = ("preset.toml", "workflow.json", "workflow-api.json")
    for name in required:
        item = folder.joinpath(name)
        if not item.is_file() or (
            isinstance(item, Path) and (item.is_symlink() or item.is_junction())
        ):
            raise KomaquillError(code="invalid_configuration")
    values = tomllib.loads(folder.joinpath("preset.toml").read_text(encoding="utf-8"))
    values["api"] = json.loads(folder.joinpath("workflow-api.json").read_text(encoding="utf-8"))
    values["ui"] = json.loads(folder.joinpath("workflow.json").read_text(encoding="utf-8"))
    return GenerationPreset.model_validate(values).model_dump(mode="json")


def catalog(workspace: Path | None = None) -> dict[str, Any]:
    """Resolve packaged definitions and explicit workspace replacements."""
    resources = files("komaquill").joinpath("resources")
    assets = {
        item.name.removesuffix(".toml"): tomllib.loads(item.read_text(encoding="utf-8"))
        for item in resources.joinpath("models").iterdir()
        if item.is_file() and item.name.endswith(".toml")
    }
    presets = {
        item.name: bundle(item) for item in resources.joinpath("presets").iterdir() if item.is_dir()
    }
    if workspace is not None and (workspace / "presets").is_dir():
        for item in sorted((workspace / "presets").iterdir()):
            if item.is_dir():
                if item.is_symlink() or item.is_junction():
                    raise KomaquillError(code="invalid_configuration")
                presets[item.name] = bundle(item)
    return {"assets": assets, "presets": presets}
