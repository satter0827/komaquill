"""Acquire pinned model files through the shared verified asset downloader."""

import time
from pathlib import Path

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import ModelPreset
from komaquill.infrastructure.asset_download import download_asset
from komaquill.infrastructure.asset_store import cached_path
from komaquill.infrastructure.configuration import GpuProject, Project
from komaquill.infrastructure.diagnostics import record_event


def fetch_model(project: Project, model: ModelPreset) -> Path:
    """Reuse verified bytes and refuse remote acquisition of transferred assets."""
    started = time.monotonic()
    existing = cached_path(project.models, model)
    if existing is not None:
        record_event(project, "model", "cache_verified", asset=model.name)
        return existing
    if isinstance(project, GpuProject) and (model.transfer or model.local_only):
        raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)
    record_event(project, "model", "download_started", asset=model.name)
    result = download_asset(
        project.models, model, project.settings().request_timeout, lambda: False
    )
    record_event(
        project,
        "model",
        "download_verified",
        asset=model.name,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return result
