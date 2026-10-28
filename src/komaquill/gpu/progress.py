"""Record safe preparation phases in the temporary runtime."""

import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.paths import GpuPaths


def progress(
    root: Path, phase: str, asset: str | None = None, failure: dict[str, object] | None = None
) -> None:
    """Publish the current phase without capturing command output or credentials."""
    path = GpuPaths(root).logs / "progress.json"
    previous = {}
    with suppress(OSError, ValueError, AttributeError):
        previous = json.loads(path.read_bytes())
    if isinstance(previous, dict) and previous.get("phase") == "failed":
        if failure is not None and failure != previous.get("failure"):
            atomic_json(
                path,
                {
                    **previous,
                    "last_failure": failure,
                    "last_failure_at": datetime.now(UTC).isoformat(),
                },
            )
        return
    prior = previous.get("phase") if isinstance(previous, dict) else None
    atomic_json(
        path,
        {
            "phase": phase,
            **({"failure": failure, "previous_phase": prior} if failure is not None else {}),
            "asset": asset
            if asset is not None
            else previous.get("asset")
            if failure is not None and isinstance(previous, dict)
            else None,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
