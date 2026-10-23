"""Download models from their pinned upstream locations."""

import hashlib
import os
import time
from pathlib import Path

import httpx
from filelock import FileLock

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import ModelPreset
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event


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

    started = time.monotonic()
    project = Project(artifacts.parent)
    with FileLock(str(folder / ".download.lock")):
        existing = folder / model.filename
        if existing.is_file() and file_digest(existing) == model.sha256:
            record_event(
                project,
                "model",
                "cache_verified",
                asset=model.name,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            return existing
        record_event(project, "model", "download_started", asset=model.name)
        if model.url:
            target = existing
            temporary = existing.with_suffix(existing.suffix + ".part")
            try:
                with httpx.stream("GET", model.url, follow_redirects=True, timeout=60) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                if file_digest(temporary) != model.sha256:
                    raise KomaquillError(code="invalid_output")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        else:
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
            raise KomaquillError(
                display_key="text.the_model_failed_sha256_verification_check_the_downloaded"
            )
        record_event(
            project,
            "model",
            "download_verified",
            asset=model.name,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return target
