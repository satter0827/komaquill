"""Resolve provider credentials on the PC without persisting model bytes."""

from urllib.parse import urlsplit

import httpx
import keyring
from huggingface_hub import get_token

from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset


def credentials() -> dict[str, str]:
    """Read standard authentication stores without exposing their values."""
    result = {}
    if token := get_token():
        result["huggingface.co"] = token
    if token := keyring.get_password("komaquill-models", "civitai"):
        result["civitai.com"] = token
    return result


def preflight(assets: list[ModelPreset], timeout: float) -> None:
    """Verify access to pinned files before allocating a GPU."""
    tokens = credentials()
    for asset in assets:
        url = (
            asset.url
            or f"https://huggingface.co/{asset.repo_id}/resolve/{asset.revision}/{asset.filename}"
        )
        host = urlsplit(url).hostname or ""
        headers = {"User-Agent": "Komaquill/0.1", "Range": "bytes=0-0"}
        if host in tokens:
            headers["Authorization"] = f"Bearer {tokens[host]}"
        try:
            with httpx.stream(
                "GET", url, headers=headers, timeout=timeout, follow_redirects=True
            ) as response:
                response.raise_for_status()
                if "text/html" in response.headers.get("content-type", ""):
                    raise KomaquillError(
                        code="authentication_failed", stage="preparation", retryable=False
                    )
        except httpx.HTTPError as error:
            status = (
                error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            )
            raise KomaquillError(
                code="authentication_failed" if status in {401, 403} else "http_failed",
                stage="preparation",
                http_status=status,
            ) from None
