"""Assemble Python distributions with the prebuilt browser application."""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parent = ROOT / ".artifacts/build/packages"
    if parent.resolve() != parent:
        raise ValueError("The package staging directory must be inside the workspace.")
    parent.mkdir(parents=True, exist_ok=True)
    uv = (
        os.environ.get("KOMAQUILL_UV")
        or shutil.which("uv")
        or str(ROOT / ".artifacts/runtime/uv/uv.exe")
    )
    with TemporaryDirectory(dir=parent) as temporary:
        staging = Path(temporary)
        shutil.copytree(
            ROOT / "src/backend",
            staging,
            ignore=shutil.ignore_patterns("__pycache__", ".artifacts", "tests"),
            dirs_exist_ok=True,
        )
        shutil.copytree(ROOT / ".artifacts/build/frontend", staging / "src/komaquill/resources/web")
        subprocess.run(
            [uv, "build", "--project", str(staging), "--out-dir", str(ROOT / ".artifacts/dist")],
            check=True,
            env={**os.environ, "UV_CACHE_DIR": str(ROOT / ".artifacts/cache/uv")},
        )


if __name__ == "__main__":
    main()
