"""Identify executable package contents independently of the workspace repository."""

import hashlib
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

RESOURCE_NAMES = (
    "defaults.toml",
    "model.toml",
    "runtime.toml",
    "workflow.json",
    "workflow-api.json",
    "worker-requirements.txt",
)


def package_contents() -> Iterator[tuple[str, bytes]]:
    """Include Python modules and explicitly published package resources only."""
    anchor = files("komaquill")

    def modules(folder: Traversable, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        for item in sorted(folder.iterdir(), key=lambda row: row.name):
            relative = prefix + item.name
            if item.is_dir() and not item.name.startswith(".") and item.name != "__pycache__":
                yield from modules(item, relative + "/")
            elif item.is_file() and item.name.endswith(".py"):
                yield "komaquill/" + relative, item.read_bytes()

    yield from modules(anchor)
    for name in RESOURCE_NAMES:
        yield "komaquill/resources/" + name, anchor.joinpath("resources", name).read_bytes()


def code_sha256(entries: Iterable[tuple[str, bytes]] | None = None) -> str:
    """Hash both names and bytes to identify the actual executable package."""
    digest = hashlib.sha256()
    for name, content in package_contents() if entries is None else entries:
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def source_revision() -> str | None:
    """Report a development checkout's commit when available; never require Git."""
    root = Path(str(files("komaquill"))).parent.parent
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None
