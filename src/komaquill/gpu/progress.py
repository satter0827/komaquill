"""Record safe preparation phases in the temporary runtime."""

from datetime import UTC, datetime
from pathlib import Path

from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.paths import GpuPaths


def progress(root: Path, phase: str, asset: str | None = None) -> None:
    """Publish the current phase without capturing command output or credentials."""
    atomic_json(
        GpuPaths(root).logs / "progress.json",
        {
            "phase": phase,
            "asset": asset,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
