"""Download models from their pinned upstream locations."""

import os
import sys
import time
from pathlib import Path

import httpx
from filelock import FileLock
from huggingface_hub.errors import HfHubHTTPError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import ModelPreset
from komaquill.infrastructure.asset_store import cached_path, register
from komaquill.infrastructure.asset_store import file_digest as file_digest
from komaquill.infrastructure.atomic import discard_temporary
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event


def fetch_model(project: Project, model: ModelPreset) -> Path:
    """Download a model and verify its SHA256."""
    folder = project.models / model.role / model.sha256
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(project.cache / "huggingface")
    os.environ["HF_XET_CACHE"] = str(project.cache / "xet")
    from huggingface_hub import hf_hub_download

    started = time.monotonic()
    with FileLock(str(folder / ".download.lock")):
        existing = folder / model.filename
        if cached_path(project.models, model) is not None:
            record_event(
                project,
                "model",
                "cache_verified",
                asset=model.name,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            return existing
        record_event(project, "model", "download_started", asset=model.name)
        verified = False
        acquired_at = time.monotonic()
        try:
            if model.url:
                target = existing
                temporary = existing.with_suffix(existing.suffix + ".part")
                try:
                    with httpx.stream(
                        "GET",
                        model.url,
                        follow_redirects=True,
                        timeout=project.settings().request_timeout,
                    ) as response:
                        response.raise_for_status()
                        with temporary.open("wb") as handle:
                            for chunk in response.iter_bytes():
                                handle.write(chunk)
                    record_event(
                        project,
                        "model",
                        "download_finished",
                        asset=model.name,
                        elapsed_seconds=round(time.monotonic() - acquired_at, 3),
                    )
                    verify_started = time.monotonic()
                    if file_digest(temporary) != model.sha256:
                        raise KomaquillError(code="invalid_output")
                    verified = True
                    record_event(
                        project,
                        "model",
                        "hash_verified",
                        asset=model.name,
                        elapsed_seconds=round(time.monotonic() - verify_started, 3),
                    )
                    temporary.replace(target)
                finally:
                    if temporary.exists():
                        discard_temporary(temporary, sys.exception())
            else:
                target = Path(
                    hf_hub_download(
                        repo_id=model.repo_id,
                        filename=model.filename,
                        revision=model.revision,
                        local_dir=folder,
                        cache_dir=project.cache / "huggingface" / "hub",
                        force_download=existing.is_file(),
                    )
                )
        except (httpx.HTTPError, HfHubHTTPError) as error:
            failed_response = getattr(error, "response", None)
            status = getattr(failed_response, "status_code", None)
            raise KomaquillError(
                code="http_failed",
                stage="preparation",
                http_status=status if isinstance(status, int) else None,
            ) from None
        if not verified:
            record_event(
                project,
                "model",
                "download_finished",
                asset=model.name,
                elapsed_seconds=round(time.monotonic() - acquired_at, 3),
            )
        verify_started = time.monotonic()
        if not verified and file_digest(target) != model.sha256:
            raise KomaquillError(
                display_key="text.the_model_failed_sha256_verification_check_the_downloaded"
            )
        if not verified:
            record_event(
                project,
                "model",
                "hash_verified",
                asset=model.name,
                elapsed_seconds=round(time.monotonic() - verify_started, 3),
            )
        record_event(
            project,
            "model",
            "download_verified",
            asset=model.name,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        register(project.models, model, target)
        return target
