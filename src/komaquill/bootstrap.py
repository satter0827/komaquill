"""Prepare uv and pinned dependencies on the GPU runtime."""

import hashlib
import os
import subprocess
import tarfile
import tomllib
import urllib.request
from pathlib import Path


def main() -> None:
    """Start an isolated environment without modifying system Python."""
    root = Path(__file__).resolve().parents[1]
    artifacts = root / ".artifacts"
    tools = artifacts / "runtime/tools"
    tools.mkdir(parents=True, exist_ok=True)
    config = tomllib.loads((root / "komaquill/resources/runtime.toml").read_text())
    uv = tools / "uv"
    print("実行環境を準備しています。", flush=True)
    if not uv.is_file():
        asset = "uv-x86_64-unknown-linux-gnu.tar.gz"
        base = f"https://github.com/astral-sh/uv/releases/download/{config['uv_version']}/{asset}"
        archive = tools / asset
        with urllib.request.urlopen(base, timeout=60) as response:
            archive.write_bytes(response.read())
        with urllib.request.urlopen(base + ".sha256", timeout=60) as response:
            expected = response.read().decode().split()[0]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise RuntimeError("uvのSHA256が一致しません。")
        with tarfile.open(archive) as bundle:
            member = "uv-x86_64-unknown-linux-gnu/uv"
            if member not in bundle.getnames():
                raise RuntimeError("uvが配布物にありません。")
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError("uvが配布物にありません。")
            uv.write_bytes(source.read())
        uv.chmod(0o700)
    env = dict(os.environ)
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
    # Pylock records exact artifact URLs and hashes, preserving package source affinity.
    subprocess.run(
        [
            str(uv),
            "pip",
            "sync",
            "--python",
            str(python),
            "--require-hashes",
            str(root / "komaquill/resources/pylock.worker.toml"),
        ],
        cwd=root,
        env=env,
        check=True,
    )
    print("ComfyUIとモデルを準備しています。", flush=True)
    subprocess.run(
        [str(python), "-u", "-m", "komaquill.worker", str(root)],
        cwd=root,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
