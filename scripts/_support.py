"""Keep development reporting independent of product imports."""

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})
REPLACE_TIMEOUT_SECONDS = 1.0
REPLACE_RETRY_SECONDS = 0.05


def comparison_base() -> str:
    """Use the same explicit comparison base for Nx and Python selection."""
    return os.environ.get("NX_BASE") or str(
        json.loads((ROOT / "nx.json").read_bytes())["defaultBase"]
    )


def message(key: str, **values: object) -> str:
    """Read the shared catalog without importing the package being checked."""
    try:
        data = json.loads((ROOT / "scripts/messages.json").read_bytes())
        return str(data[key]).format(**values)
    except (OSError, ValueError, KeyError):
        return "Message definitions are unavailable. Restore the source files."


def atomic_json(path: Path, data: object) -> None:
    """Publish a report without replacing the original error during cleanup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        deadline = None
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError as error:
                if (
                    sys.platform != "win32"
                    or getattr(error, "winerror", None) not in WINDOWS_REPLACE_ERRORS
                ):
                    raise
                now = time.monotonic()
                if deadline is None:
                    deadline = now + REPLACE_TIMEOUT_SECONDS
                elif now >= deadline:
                    raise
                time.sleep(REPLACE_RETRY_SECONDS)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def source_revision(root: Path = ROOT) -> str | None:
    """Return the local revision without printing Git diagnostics."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def input_fingerprint(root: Path) -> str:
    """Fingerprint source inputs while excluding generated storage and bytecode."""
    digest = hashlib.sha256()
    names = ("src", "scripts", "tests", "bootstrap", "docs", ".vscode", ".github", ".githooks")
    paths: list[Path] = []
    for name in names:
        for folder, children, files in os.walk(root / name):
            children[:] = [
                name
                for name in children
                if name not in {"node_modules", "__pycache__", ".artifacts", ".git"}
            ]
            paths.extend(Path(folder) / name for name in files)
    paths.extend(p for p in root.iterdir() if p.is_file() and p.name != ".DS_Store")
    for path in sorted(set(paths)):
        if "__pycache__" in path.parts or path.suffix == ".pyc" or path.is_symlink():
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def policy(root: Path) -> dict[str, Any]:
    """Load development execution limits from the project configuration."""
    import tomllib

    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    return dict(
        tomllib.loads(path.read_text(encoding="utf-8"))["tool"]["komaquill"].get("quality", {})
    )


def freshness(root: Path, level: int) -> dict[str, object]:
    """Describe retained evidence without rewriting it or treating stale success as current."""
    current = input_fingerprint(root)
    folder = root / ".artifacts/records/reports/quality"
    result: dict[str, object] = {"level": level, "input_sha256": current}
    for kind in ("attempt", "success"):
        path = folder / f"latest-{kind}-l{level}.json"
        try:
            value = json.loads(path.read_bytes())
            result[kind] = {**value, "matches_current_inputs": value["input_sha256"] == current}
        except (OSError, ValueError, KeyError, TypeError):
            result[kind] = None
    return result
