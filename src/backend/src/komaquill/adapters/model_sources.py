"""Resolve public model metadata to immutable sources before confirmation."""

import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from huggingface_hub import get_hf_file_metadata, hf_hub_url

from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset

SAFE_SUFFIXES = frozenset({".safetensors", ".sft"})


def resolve_model(name: str, role: str, url: str, timeout: float) -> ModelPreset:
    """Accept public Hugging Face files and versioned Civitai downloads only."""
    from pathlib import PurePosixPath

    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or PurePosixPath(name).suffix.lower() not in SAFE_SUFFIXES
    ):
        raise KomaquillError(code="unsupported_asset", retryable=False)
    try:
        if parsed.hostname == "huggingface.co" and not parsed.query:
            parts = unquote(parsed.path).strip("/").split("/")
            if len(parts) < 5 or parts[2] != "resolve":
                raise ValueError
            repo = "/".join(parts[:2])
            remote_file = "/".join(parts[4:])
            if PurePosixPath(remote_file).name != name:
                raise ValueError
            metadata = get_hf_file_metadata(url, token=False, timeout=timeout)
            revision = metadata.commit_hash
            sha256 = metadata.etag
            if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
                raise ValueError
            # A resolved URL keeps nested repository paths without changing the local name.
            fixed_url = hf_hub_url(repo, remote_file, revision=revision)
            size = metadata.size
        elif parsed.hostname == "civitai.com":
            match = re.fullmatch(r"/api/download/models/([0-9]+)", parsed.path)
            if not match or not set(parse_qs(parsed.query)).issubset(
                {"type", "format", "size", "fp", "fileId"}
            ):
                raise ValueError
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.get(f"https://civitai.com/api/v1/model-versions/{match[1]}")
                response.raise_for_status()
                data = response.json()
            candidates = [f for f in data["files"] if f.get("name") == name]
            if len(candidates) != 1:
                raise ValueError
            file = candidates[0]
            requested_ids = parse_qs(parsed.query).get("fileId")
            if requested_ids and requested_ids != [str(file["id"])]:
                raise ValueError
            sha256 = file["hashes"]["SHA256"].lower()
            fixed_url = file["downloadUrl"]
            size = None  # Civitai reports rounded KB; exact byte size is not known.
            fixed = urlsplit(fixed_url)
            if fixed.hostname != "civitai.com" or fixed.path != parsed.path:
                raise ValueError
        else:
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            raise ValueError
        assert isinstance(sha256, str)
        return ModelPreset.model_validate(
            {
                "name": "sha-" + sha256[:16],
                "role": role,
                "url": fixed_url,
                "filename": name,
                "sha256": sha256,
                "size_bytes": size,
                "license": "Review the upstream model license",
                "license_url": url,
            }
        )
    except (ValueError, KeyError, TypeError, httpx.HTTPError):
        raise KomaquillError(
            code="unsupported_asset", stage="preparation", retryable=False
        ) from None
