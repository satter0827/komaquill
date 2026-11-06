"""Install the selected wheel once for isolated CLI-to-browser acceptance."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from filelock import FileLock

from tools.reporting import ROOT, message


def prepare(wheel: Path | None = None) -> Path:
    wheels = [wheel] if wheel else list((ROOT / ".artifacts/dist").glob("komaquill-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(message("quality.select_one_wheel"))
    wheel = wheels[0]
    digest = hashlib.sha256(
        wheel.read_bytes() + (ROOT / "src/backend/uv.lock").read_bytes() + sys.version.encode()
    ).hexdigest()
    directory = ROOT / ".artifacts/runtime/browser-tests" / digest
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    uv = os.environ.get("KOMAQUILL_UV") or shutil.which("uv")
    if not uv:
        raise RuntimeError(message("quality.install_dependencies"))
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory) + ".lock"):
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
    return python


if __name__ == "__main__":
    print(prepare())
