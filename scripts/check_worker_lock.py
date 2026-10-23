"""Check that the packaged GPU requirements match the canonical uv lock."""

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main(*, write: bool = False) -> None:
    """Export without resolving new versions and compare exact normalized content."""
    root = Path(__file__).resolve().parents[1]
    executable = os.environ.get("KOMAQUILL_UV") or shutil.which("uv")
    if not executable:
        raise SystemExit("uvが見つかりません。開発環境を有効にしてください。")
    temporary = root / ".artifacts/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="worker-lock-", dir=temporary) as folder:
        exported = Path(folder) / "pylock.worker.toml"
        subprocess.run(
            [
                executable,
                "export",
                "--locked",
                "--extra",
                "worker",
                "--no-dev",
                "--no-emit-project",
                "--no-header",
                "--format",
                "pylock.toml",
                "--output-file",
                str(exported),
                "--quiet",
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        expected = root / "src/komaquill/resources/pylock.worker.toml"
        content = "".join(
            line.rstrip("\n") + " # pragma: allowlist secret Public artifact SHA256 from uv.lock.\n"
            if "hashes = { sha256 =" in line
            else line
            for line in exported.read_text(encoding="utf-8").splitlines(keepends=True)
        )
        if write:
            expected.write_text(content, encoding="utf-8")
        if content != expected.read_text(encoding="utf-8"):
            raise SystemExit(
                "GPU依存の固定定義がuv.lockと一致しません。依存定義を書き出し直してください。"
            )
    print("GPU依存とuv.lockの一致を確認しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPU依存の固定定義を検査します。")
    parser.add_argument("--write", action="store_true", help="uv.lockから固定定義を書き出します。")
    main(write=parser.parse_args().write)
