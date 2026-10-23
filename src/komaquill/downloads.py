"""Download models from their pinned upstream locations."""

import hashlib
import os
from pathlib import Path

from filelock import FileLock

from komaquill.errors import KomaquillError
from komaquill.models import ModelPreset


def file_digest(path: Path) -> str:
    """Verify large files using bounded chunks."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fetch_model(artifacts: Path, model: ModelPreset) -> Path:
    """Download a model and verify its SHA256."""
    folder = artifacts / "models" / model.name
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(artifacts / "cache" / "huggingface")
    os.environ["HF_XET_CACHE"] = str(artifacts / "cache" / "xet")
    from huggingface_hub import hf_hub_download

    with FileLock(str(folder / ".download.lock")):
        existing = folder / model.filename
        if existing.is_file() and file_digest(existing) == model.sha256:
            return existing
        target = Path(
            hf_hub_download(
                repo_id=model.repo_id,
                filename=model.filename,
                revision=model.revision,
                local_dir=folder,
                cache_dir=artifacts / "cache" / "huggingface" / "hub",
                force_download=existing.is_file(),
            )
        )
        if file_digest(target) != model.sha256:
            raise KomaquillError("モデルのSHA256が一致しません。ファイルを確認してください。")
        return target
