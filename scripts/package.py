"""Assemble Python distributions with the prebuilt browser application."""

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    staging = ROOT / ".artifacts/build/packages" / uuid4().hex
    shutil.copytree(
        ROOT / "src/backend",
        staging,
        ignore=shutil.ignore_patterns("__pycache__", ".artifacts", "tests"),
    )
    shutil.copytree(ROOT / ".artifacts/build/frontend", staging / "src/komaquill/resources/web")
    uv = (
        os.environ.get("KOMAQUILL_UV")
        or shutil.which("uv")
        or str(ROOT / ".artifacts/runtime/uv/uv.exe")
    )
    subprocess.run(
        [uv, "build", "--project", str(staging), "--out-dir", str(ROOT / ".artifacts/dist")],
        check=True,
        env={**os.environ, "UV_CACHE_DIR": str(ROOT / ".artifacts/cache/uv")},
    )


if __name__ == "__main__":
    main()
