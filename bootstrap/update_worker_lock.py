"""Update the packaged GPU lock from the canonical uv lock."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.checks.worker_lock import exported


def main() -> None:
    """Write the explicit dependency-update output only on request."""
    root = Path(__file__).resolve().parents[1]
    content = exported(root)
    (root / "src/backend/src/komaquill/resources/runtime/pylock.worker.toml").write_text(
        content, encoding="utf-8"
    )


if __name__ == "__main__":
    main()
