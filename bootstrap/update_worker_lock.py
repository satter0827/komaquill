"""Update the packaged GPU lock from the canonical uv lock."""

from pathlib import Path

from scripts.checks.worker_lock import exported


def main() -> None:
    """Write the explicit dependency-update output only on request."""
    root = Path(__file__).resolve().parents[1]
    content = exported(root)
    (root / "src/komaquill/resources/runtime/pylock.worker.toml").write_text(
        content, encoding="utf-8"
    )


if __name__ == "__main__":
    main()
