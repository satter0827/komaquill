"""Store verified models by purpose and digest without filename collisions."""

import hashlib
import json
from pathlib import Path

from komaquill.domain.presets import ModelPreset
from komaquill.infrastructure.storage import atomic_json


def file_digest(path: Path) -> str:
    """Compute a SHA256 using bounded reads."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def model_path(artifacts: Path, model: ModelPreset) -> Path:
    """Resolve a validated asset beneath the model store."""
    return artifacts / "models" / model.role / model.sha256 / model.filename


def register(artifacts: Path, model: ModelPreset, path: Path) -> None:
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


def verified_cache(artifacts: Path, model: ModelPreset) -> bool:
    """Reuse an unchanged verified file without re-reading gigabytes."""
    path = model_path(artifacts, model)
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


def verify_file(artifacts: Path, model: ModelPreset) -> bool:
    """Explicitly verify bytes and refresh their cache evidence."""
    path = model_path(artifacts, model)
    if not path.is_file() or file_digest(path) != model.sha256:
        return False
    register(artifacts, model, path)
    return True


def inventory(artifacts: Path) -> list[ModelPreset]:
    """Read validated model records without hashing their contents again."""
    result = []
    for path in (artifacts / "models").glob("*/*/asset.json"):
        record = json.loads(path.read_bytes())
        model = ModelPreset.model_validate(record["asset"])
        if path.parent == model_path(artifacts, model).parent and verified_cache(artifacts, model):
            result.append(model)
    return result
