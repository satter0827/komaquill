"""Identify executable package contents independently of the workspace repository."""

import ast
import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

RESOURCE_NAMES = (
    "messages.json",
    "defaults.toml",
    "presets.toml",
    "sd15.json",
    "sd15-api.json",
    "pony-preview.json",
    "pony-preview-api.json",
    "pony-upscale.json",
    "pony-upscale-api.json",
    "runtime.toml",
    "workflow.json",
    "workflow-api.json",
    "pylock.worker.toml",
    "web/extension.js",
    "web/preparation.js",
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


GPU_MODULES = frozenset(
    {
        "__init__.py",
        "messages.py",
        "domain/__init__.py",
        "domain/configuration.py",
        "domain/models.py",
        "domain/errors.py",
        "domain/workflow.py",
        "domain/presets.py",
        "gpu/__init__.py",
        "gpu/bootstrap.py",
        "gpu/progress.py",
        "gpu/extension.py",
        "domain/assets.py",
        "domain/materials.py",
        "gpu/preservation.py",
        "infrastructure/asset_download.py",
        "infrastructure/asset_store.py",
        "gpu/worker.py",
        "infrastructure/__init__.py",
        "infrastructure/configuration.py",
        "infrastructure/paths.py",
        "infrastructure/storage.py",
        "infrastructure/diagnostics.py",
        "infrastructure/downloads.py",
        "infrastructure/workflows.py",
        "infrastructure/provenance.py",
    }
)


def gpu_contents() -> Iterator[tuple[str, bytes]]:
    """Select the declared worker modules and only their referenced presentation text."""
    entries = [
        (name, content)
        for name, content in package_contents()
        if name.removeprefix("komaquill/") in GPU_MODULES
        or (name.startswith("komaquill/resources/") and not name.endswith("messages.json"))
    ]
    anchor = files("komaquill")
    all_text = json.loads(anchor.joinpath("resources/messages.json").read_text(encoding="utf-8"))
    keys = {key for key in all_text if key.startswith(("error.", "ui."))}
    for name, content in entries:
        if not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "message" and node.args:
                    argument = node.args[0]
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        keys.add(argument.value)
                for keyword in node.keywords:
                    if keyword.arg == "display_key" and isinstance(keyword.value, ast.Constant):
                        keys.add(keyword.value.value)
    entries.append(
        (
            "komaquill/resources/messages.json",
            json.dumps({key: all_text[key] for key in sorted(keys)}, sort_keys=True).encode(),
        )
    )
    yield from sorted(entries)
