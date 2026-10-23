"""Download confirmed assets with resumable partial files and cancellation."""

import os
import re
from collections.abc import Callable
from pathlib import Path

import httpx
from filelock import FileLock

from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset
from komaquill.infrastructure.asset_store import model_path, register, verified_cache
from komaquill.infrastructure.downloads import file_digest

CHUNK_BYTES = 1024 * 1024
DISK_RESERVE_BYTES = 64 * 1024 * 1024


def download_asset(
    artifacts: Path,
    asset: ModelPreset,
    timeout: float,
    cancelled: Callable[[], bool],
) -> Path:
    """Keep incomplete bytes for retry, publishing only a verified final file."""
    import shutil

    target = model_path(artifacts, asset)
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target.parent / ".download.lock")):
        if verified_cache(artifacts, asset):
            return target
        if target.is_file() and file_digest(target) == asset.sha256:
            register(artifacts, asset, target)
            return target
        if asset.local_only:
            raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)
        partial = target.with_suffix(target.suffix + ".part")
        size = partial.stat().st_size if partial.is_file() else 0
        remaining = max(0, (asset.size_bytes or 0) - size)
        if shutil.disk_usage(target.parent).free < remaining + DISK_RESERVE_BYTES:
            raise KomaquillError(code="storage_failed", stage="preparation")
        if cancelled():
            raise KomaquillError(code="stopping", stage="preparation", retryable=False)
        url = (
            asset.url
            or f"https://huggingface.co/{asset.repo_id}/resolve/{asset.revision}/{asset.filename}"
        )
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"Range": f"bytes={size}-"} if size else {},
                timeout=timeout,
                follow_redirects=True,
            ) as response:
                if response.status_code == 416:
                    partial.unlink(missing_ok=True)
                    raise KomaquillError(code="http_failed", stage="preparation")
                response.raise_for_status()
                append = size > 0 and response.status_code == 206
                if append and not re.match(
                    rf"bytes {size}-[0-9]+/[0-9]+$", response.headers.get("Content-Range", "")
                ):
                    raise KomaquillError(code="invalid_response", stage="preparation")
                with partial.open("ab" if append else "wb") as handle:
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        if cancelled():
                            raise KomaquillError(
                                code="stopping", stage="preparation", retryable=False
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        except httpx.HTTPError as error:
            status = (
                error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            )
            raise KomaquillError(
                code="http_failed", stage="preparation", http_status=status
            ) from None
        if file_digest(partial) != asset.sha256:
            partial.unlink(missing_ok=True)
            raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)
        partial.replace(target)
        register(artifacts, asset, target)
        return target
