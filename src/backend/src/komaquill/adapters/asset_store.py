"""Store verified models by purpose and digest without filename collisions."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from komaquill.adapters.artifacts import file_digest as file_digest
from komaquill.adapters.atomic import atomic_json
from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset


def model_path(models: Path, model: ModelPreset) -> Path:
    """Resolve a validated asset beneath the model store."""
    return models / model.role / model.sha256 / model.filename


def register(models: Path, model: ModelPreset, path: Path) -> None:
    """Record provenance only after the caller verifies the file bytes."""
    info = path.stat()
    atomic_json(
        path.parent / "asset.json",
        {
            "format_version": 1,
            "asset": model.model_dump(mode="json"),
            "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        },
    )


def verified_cache(models: Path, model: ModelPreset) -> bool:
    """Reuse an unchanged verified file without re-reading gigabytes."""
    path = model_path(models, model)
    try:
        record = json.loads((path.parent / "asset.json").read_bytes())
        info = path.stat()
        return bool(
            record["asset"]["sha256"] == model.sha256
            and record["asset"]["filename"] == model.filename
            and record["size_bytes"] == info.st_size
            and record["mtime_ns"] == info.st_mtime_ns
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def verify_file(models: Path, model: ModelPreset) -> bool:
    """Explicitly verify bytes and refresh their cache evidence."""
    path = model_path(models, model)
    if not path.is_file() or file_digest(path) != model.sha256:
        return False
    register(models, model, path)
    return True


class AssetRecord(BaseModel):
    format_version: Literal[1]
    asset: ModelPreset
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)


def inventory(models: Path, *, hashes: set[str] | None = None) -> list[ModelPreset]:
    """Read validated model records without hashing their contents again."""
    result = []
    for path in models.glob("*/*/asset.json"):
        if hashes is not None and path.parent.name not in hashes:
            continue
        try:
            record = AssetRecord.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            raise KomaquillError(code="storage_failed", stage="storage", retryable=False) from None
        model = record.asset
        if path.parent == model_path(models, model).parent and verified_cache(models, model):
            result.append(model)
    return result


def cached_path(models: Path, model: ModelPreset) -> Path | None:
    """Share cache verification and registration across acquisition transports."""
    target = model_path(models, model)
    if verified_cache(models, model):
        return target
    if target.is_file() and file_digest(target) == model.sha256:
        register(models, model, target)
        return target
    return None
