"""Prepare uv and pinned dependencies on the GPU runtime."""

import hashlib
import os
import subprocess
import tarfile
import tomllib
import urllib.request
from pathlib import Path

from komaquill.gpu.progress import progress
from komaquill.messages import message


def main() -> None:
    """Start an isolated environment without modifying system Python."""
    root = Path(__file__).resolve().parents[2]
    progress(root, "environment")
    artifacts = root / ".artifacts"
    tools = artifacts / "runtime/tools"
    tools.mkdir(parents=True, exist_ok=True)
    config = tomllib.loads((root / "komaquill/resources/runtime/versions.toml").read_text())
    uv = tools / "uv"
    print(message("text.preparing_the_runtime_environment"), flush=True)
    if not uv.is_file():
        asset = "uv-x86_64-unknown-linux-gnu.tar.gz"
        base = f"https://github.com/astral-sh/uv/releases/download/{config['uv_version']}/{asset}"
        archive = tools / asset
        with urllib.request.urlopen(base, timeout=60) as response:
            archive.write_bytes(response.read())
        with urllib.request.urlopen(base + ".sha256", timeout=60) as response:
            expected = response.read().decode().split()[0]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise RuntimeError(message("text.the_uv_archive_failed_sha256_verification"))
        with tarfile.open(archive) as bundle:
            member = "uv-x86_64-unknown-linux-gnu/uv"
            if member not in bundle.getnames():
                raise RuntimeError(message("text.the_uv_executable_is_missing_from_the_archive"))
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(message("text.the_uv_executable_is_missing_from_the_archive"))
            uv.write_bytes(source.read())
        uv.chmod(0o700)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    for key, relative in {
        "UV_PROJECT_ENVIRONMENT": "runtime/venv",
        "UV_PYTHON_INSTALL_DIR": "runtime/python",
        "UV_CACHE_DIR": "cache/uv",
        "PYTHONPYCACHEPREFIX": "cache/pycache",
        "HF_HOME": "cache/huggingface",
        "HF_XET_CACHE": "cache/xet",
        "XDG_CACHE_HOME": "cache/xdg",
        "XDG_DATA_HOME": "cache/xdg-data",
        "XDG_CONFIG_HOME": "runtime/xdg-config",
        "TMPDIR": "tmp",
    }.items():
        env[key] = str(artifacts / relative)
    (artifacts / "tmp").mkdir(exist_ok=True)
    python = artifacts / "runtime/venv/bin/python"
    subprocess.run(
        [str(uv), "venv", "--python", config["python_version"], str(python.parent.parent)],
        cwd=root,
        env=env,
        check=True,
    )
    progress(root, "dependencies")
    # Pylock records exact artifact URLs and hashes, preserving package source affinity.
    subprocess.run(
        [
            str(uv),
            "pip",
            "sync",
            "--python",
            str(python),
            "--require-hashes",
            str(root / "komaquill/resources/runtime/pylock.worker.toml"),
        ],
        cwd=root,
        env=env,
        check=True,
    )
    progress(root, "comfyui")
    print(message("text.preparing_comfyui_and_the_model"), flush=True)
    subprocess.run(
        [str(python), "-u", "-m", "komaquill.gpu.worker", str(root)],
        cwd=root,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
