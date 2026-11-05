"""Verify local file contents with bounded memory and no storage dependencies."""

import hashlib
from pathlib import Path


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
