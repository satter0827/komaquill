"""Verify fixed bootstrap tools locally before allocating a remote runtime."""

import hashlib
import tomllib
from importlib.resources import files

import httpx

from komaquill.adapters.atomic import atomic_bytes
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.errors import KomaquillError

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def runtime_archive(project: RuntimeWorkspace) -> bytes:
    settings = tomllib.loads(
        files("komaquill").joinpath("resources/runtime/versions.toml").read_text(encoding="utf-8")
    )
    expected = settings["uv_linux_sha256"]
    cache = project.cache / "runtime-tools" / (expected + ".tar.gz")
    if cache.is_file():
        content: bytes = cache.read_bytes()
        if hashlib.sha256(content).hexdigest() == expected:
            return content
    url = f"https://github.com/astral-sh/uv/releases/download/{settings['uv_version']}/uv-x86_64-unknown-linux-gnu.tar.gz"
    chunks, size = [], 0
    with httpx.stream(
        "GET", url, timeout=project.settings().request_timeout, follow_redirects=True
    ) as response:
        if response.status_code != 200:
            raise KomaquillError(
                code="http_failed",
                stage="preparation",
                http_status=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)
            chunks.append(chunk)
    content = b"".join(chunks)
    if hashlib.sha256(content).hexdigest() != expected:
        raise KomaquillError(code="identity_mismatch", stage="preparation", retryable=False)
    atomic_bytes(cache, content)
    return content
