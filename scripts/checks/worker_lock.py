"""Check that the packaged GPU requirements match the canonical uv lock."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from scripts._support import message


def exported(root: Path) -> str:
    """Export without resolving new versions and compare exact normalized content."""
    executable = os.environ.get("KOMAQUILL_UV") or shutil.which("uv")
    if not executable:
        raise SystemExit(message("quality.uv_is_unavailable_activate_the_development_environment"))
    temporary = root / ".artifacts/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="worker-lock-", dir=temporary) as folder:
        exported = Path(folder) / "pylock.worker.toml"
        subprocess.run(
            [
                executable,
                "export",
                "--locked",
                "--only-group",
                "worker",
                "--no-emit-project",
                "--no-header",
                "--format",
                "pylock.toml",
                "--output-file",
                str(exported),
                "--quiet",
            ],
            cwd=root / "src/backend",
            check=True,
            stdout=subprocess.DEVNULL,
        )
        content = "".join(
            line.rstrip("\n") + " # pragma: allowlist secret Public artifact SHA256 from uv.lock.\n"
            if "hashes = { sha256 =" in line
            else line
            for line in exported.read_text(encoding="utf-8").splitlines(keepends=True)
        )
        return content


def main() -> None:
    """Compare the exported lock without modifying tracked files."""
    root = Path(__file__).resolve().parents[2]
    expected = root / "src/backend/src/komaquill/resources/runtime/pylock.worker.toml"
    if exported(root) != expected.read_text(encoding="utf-8"):
        raise SystemExit(message("quality.gpu_dependencies_differ_from_uv_lock_export_the"))
    print(message("quality.gpu_dependencies_match_uv_lock"))


if __name__ == "__main__":
    main()
