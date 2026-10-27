"""Download confirmed assets with resumable partial files and cancellation."""

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from filelock import FileLock, Timeout

from komaquill.domain.errors import FailureInfo, KomaquillError, failure_info
from komaquill.domain.presets import ModelPreset
from komaquill.infrastructure.asset_store import cached_path, file_digest, model_path, register

CHUNK_BYTES = 1024 * 1024
DISK_RESERVE_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 1.0


def download_asset(
    models: Path,
    asset: ModelPreset,
    timeout: float,
    cancelled: Callable[[], bool],
    credentials: dict[str, str] | None = None,
    on_retry: Callable[[FailureInfo], object] | None = None,
) -> Path:
    """Recover bounded transient GET failures using the same pinned partial file."""
    for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
        try:
            return _download_asset(models, asset, timeout, cancelled, credentials)
        except KomaquillError as error:
            if (
                not error.retryable
                or error.code != "http_failed"
                or attempt + 1 == MAX_DOWNLOAD_ATTEMPTS
            ):
                raise
            if cancelled():
                raise KomaquillError(
                    code="stopping", stage="preparation", retryable=False
                ) from None
            if on_retry is not None:
                on_retry(failure_info(error, "preparation"))
            time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
    raise AssertionError("The bounded download did not terminate.")


def _download_asset(
    models: Path,
    asset: ModelPreset,
    timeout: float,
    cancelled: Callable[[], bool],
    credentials: dict[str, str] | None = None,
) -> Path:
    """Keep incomplete bytes for retry, publishing only a verified final file."""
    import shutil

    target = model_path(models, asset)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(target.parent / ".download.lock"))
    while True:
        if cancelled():
            raise KomaquillError(code="stopping", stage="preparation", retryable=False)
        try:
            lock.acquire(timeout=0.25)
            break
        except Timeout:
            continue
    try:
        if (existing := cached_path(models, asset)) is not None:
            return existing
        if asset.local_only:
            raise KomaquillError(code="unsupported_asset", stage="preparation", retryable=False)
        partial = target.with_suffix(target.suffix + ".part")
        url = (
            asset.url
            or f"https://huggingface.co/{asset.repo_id}/resolve/{asset.revision}/{asset.filename}"
        )
        size = partial.stat().st_size if partial.is_file() else 0
        if size and size == asset.size_bytes:
            if file_digest(partial) != asset.sha256:
                partial.unlink(missing_ok=True)
                raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)
            partial.replace(target)
            register(models, asset, target)
            return target
        remaining = max(0, (asset.size_bytes or 0) - size)
        if shutil.disk_usage(target.parent).free < remaining + DISK_RESERVE_BYTES:
            raise KomaquillError(code="storage_failed", stage="preparation")
        if cancelled():
            raise KomaquillError(code="stopping", stage="preparation", retryable=False)
        host = urlsplit(url).hostname or ""
        try:
            with httpx.stream(
                "GET",
                url,
                headers={
                    "User-Agent": "Komaquill/0.1",
                    **(
                        {"Authorization": f"Bearer {credentials[host]}"}
                        if credentials and host in credentials
                        else {}
                    ),
                    **({"Range": f"bytes={size}-"} if size else {}),
                },
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
                code="authentication_failed" if status in {401, 403} else "http_failed",
                stage="preparation",
                http_status=status,
                retryable=status is None or status in {408, 429, 500, 502, 503, 504},
            ) from None
        if file_digest(partial) != asset.sha256:
            partial.unlink(missing_ok=True)
            raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)
        partial.replace(target)
        register(models, asset, target)
        return target
    finally:
        lock.release()


def read_credentials() -> dict[str, str]:
    """Read one bounded SSH message without retaining or echoing provider secrets."""
    import json
    import sys

    try:
        raw = sys.stdin.buffer.readline(65536)
        if not raw.endswith(b"\n"):
            raise ValueError
        result = json.loads(raw)
        if not isinstance(result, dict) or any(
            key not in {"huggingface.co", "civitai.com"}
            or not isinstance(value, str)
            or not value
            or any(c in value for c in "\r\n")
            for key, value in result.items()
        ):
            raise ValueError
        return result
    except (ValueError, UnicodeError, OSError):
        raise KomaquillError(
            code="authentication_failed", stage="preparation", retryable=False
        ) from None
