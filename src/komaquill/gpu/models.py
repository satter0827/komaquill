"""Execute an already confirmed preparation job on the owned runtime."""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from filelock import FileLock, Timeout

from komaquill.domain.configuration import RuntimeSettings
from komaquill.infrastructure.asset_download import read_credentials
from komaquill.interfaces.comfyui.api import Extension


def main(root: Path, identity: UUID) -> None:
    """Keep transient provider credentials in memory for this job only."""
    manifest = json.loads((root / "manifest.json").read_bytes())
    settings = RuntimeSettings.model_validate(manifest["settings"])
    extension = Extension(root / ".artifacts", root / ".artifacts/models", settings.request_timeout)
    started = os.environ.get("KOMAQUILL_SESSION_STARTED_AT")
    if started and settings.submission_timeout is not None:
        extension.submit_until = (
            datetime.fromisoformat(started) + timedelta(seconds=settings.submission_timeout)
        ).timestamp()
    tokens = read_credentials()
    try:
        with FileLock(str(extension.path(identity)) + ".worker.lock", timeout=0):
            asyncio.run(extension.prepare(identity, tokens))
    except Timeout:
        return


if __name__ == "__main__":
    main(Path(sys.argv[1]), UUID(sys.argv[2]))
