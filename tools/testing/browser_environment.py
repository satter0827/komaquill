"""Install the selected wheel once for isolated CLI-to-browser acceptance."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import psutil
from filelock import FileLock

from tools.observations import owner_state
from tools.reporting import ROOT, atomic_json, message, policy


def plain_tree(directory: Path) -> bool:
    pending = [directory]
    while pending:
        item = pending.pop()
        metadata = item.lstat()
        if item.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            if item == directory:
                return False
            continue
        if item.is_dir():
            pending.extend(item.iterdir())
    return True


def prune(current: Path) -> None:
    """Retain recent completed generations and every active or uncertain lease."""
    count = int(policy(ROOT).get("browser_environment_retention_count", 2))
    if current.parent != ROOT / ".artifacts/runtime/browser-tests":
        raise ValueError(message("quality.invalid_retention"))
    if count < 1:
        raise ValueError(message("quality.invalid_retention"))
    generations = []
    for item in current.parent.iterdir():
        if (
            len(item.name) != 64
            or not item.is_dir()
            or item.is_symlink()
            or getattr(item.lstat(), "st_file_attributes", 0) & 0x400
        ):
            continue
        marker = item / ".installed-locked"
        if marker.is_file() and marker.read_text(encoding="ascii") == item.name:
            generations.append(item)
    generations.sort(
        key=lambda item: (item != current, -(item / ".installed-locked").stat().st_mtime)
    )
    observations = []
    for item in generations[count:]:
        reason = "unknown"
        manifest = item / ".generation.json"
        try:
            owned = json.loads(manifest.read_bytes()) == {"format_version": 1, "digest": item.name}
            if not owned or not plain_tree(item):
                continue
            active = False
            for lease in (item / ".leases").glob("*.json"):
                value = json.loads(lease.read_bytes())
                active = owner_state(
                    {"owner_pid": value["pid"], "owner_started": value["started"]}
                ) in {"alive", "unknown"}
                if active:
                    break
            if not active:
                shutil.rmtree(item)
                reason = "removed"
            else:
                reason = "active"
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            reason = "unknown"
        observations.append({"digest": item.name, "status": reason})
    atomic_json(
        current.parent / "retention.json", {"format_version": 1, "generations": observations}
    )


def prepare(wheel: Path | None = None, *, owner_pid: int | None = None) -> Path:
    wheels = [wheel] if wheel else list((ROOT / ".artifacts/dist").glob("komaquill-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(message("quality.select_one_wheel"))
    wheel = wheels[0]
    digest = hashlib.sha256(
        wheel.read_bytes() + (ROOT / "src/backend/uv.lock").read_bytes() + sys.version.encode()
    ).hexdigest()
    directory = ROOT / ".artifacts/runtime/browser-tests" / digest
    for ancestor in (directory, *directory.parents):
        if ancestor == ROOT:
            break
        if ancestor.exists() and (
            ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError(message("quality.invalid_retention"))
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    uv = os.environ.get("KOMAQUILL_UV") or shutil.which("uv")
    if not uv:
        raise RuntimeError(message("quality.install_dependencies"))
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory.parent) + ".lock"), FileLock(str(directory) + ".lock"):
        if not (directory / ".installed-locked").is_file():
            subprocess.run(
                [uv, "venv", "--python", sys.executable, str(directory)],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=60,
            )
            subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    "--no-index",
                    str(wheel),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=60,
            )
            requirements = directory / "dependencies.txt"
            subprocess.run(
                [
                    uv,
                    "export",
                    "--project",
                    str(ROOT / "src/backend"),
                    "--locked",
                    "--all-extras",
                    "--no-dev",
                    "--no-emit-project",
                    "--output-file",
                    str(requirements),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=60,
            )
            subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "-r",
                    str(requirements),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=180,
            )
            (directory / ".installed-locked").write_text(digest, encoding="ascii")
        atomic_json(directory / ".generation.json", {"format_version": 1, "digest": digest})
        owner = psutil.Process(owner_pid or os.getpid())
        atomic_json(
            directory / ".leases" / f"{owner.pid}-{owner.create_time()}.json",
            {
                "pid": owner.pid,
                "started": owner.create_time(),
            },
        )
        prune(directory)
    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-pid", type=int)
    args = parser.parse_args()
    print(prepare(owner_pid=args.owner_pid))
