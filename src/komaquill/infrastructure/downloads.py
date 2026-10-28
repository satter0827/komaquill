"""Acquire pinned model files through the shared verified asset downloader."""

import os
import time
from pathlib import Path

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import ModelPreset
from komaquill.infrastructure.asset_download import download_asset
from komaquill.infrastructure.asset_store import cached_path
from komaquill.infrastructure.configuration import GpuProject, Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.paths import GpuPaths


def fetch_model(
    project: Project, model: ModelPreset, credentials: dict[str, str] | None = None
) -> Path:
    """Acquire GPU weights with bounded recovery and safe progress events."""
    if not isinstance(project, GpuProject):
        raise KomaquillError(code="unsupported_asset", stage="preparation", retryable=False)
    paths = GpuPaths(project.root)
    started = time.monotonic()
    existing = cached_path(project.models, model)
    if existing is not None:
        record_event(paths, "model", "cache_verified", asset=model.name, sha256=model.sha256)
        return existing
    record_event(paths, "model", "download_started", asset=model.name, sha256=model.sha256)
    deadline = os.environ.get("KOMAQUILL_SUBMIT_UNTIL")

    def cancelled() -> bool:
        if deadline is not None and time.time() >= float(deadline):
            raise KomaquillError(code="preparation_timeout", stage="preparation", retryable=False)
        return False

    result = download_asset(
        project.models,
        model,
        project.settings().request_timeout,
        cancelled,
        credentials,
        lambda failure: record_event(
            paths,
            "model",
            "download_retrying",
            asset=model.name,
            sha256=model.sha256,
            failure=failure,
        ),
    )
    record_event(
        paths,
        "model",
        "download_verified",
        asset=model.name,
        sha256=model.sha256,
        byte_count=result.stat().st_size,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return result
