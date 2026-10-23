"""Check that the packaged GPU requirements match the canonical uv lock."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    """Export without resolving new versions and compare exact normalized content."""
    root = Path(__file__).resolve().parents[1]
    executable = os.environ.get("KOMAQUILL_UV") or shutil.which("uv")
    if not executable:
        raise SystemExit("uvが見つかりません。開発環境を有効にしてください。")
    temporary = root / ".artifacts/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="worker-lock-", dir=temporary) as folder:
        exported = Path(folder) / "requirements.txt"
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
                "--no-annotate",
                "--emit-index-url",
                "--format",
                "requirements-txt",
                "--output-file",
                str(exported),
                "--quiet",
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        expected = root / "src/komaquill/resources/worker-requirements.txt"
        if exported.read_text(encoding="utf-8") != expected.read_text(encoding="utf-8"):
            raise SystemExit(
                "GPU依存の固定定義がuv.lockと一致しません。依存定義を書き出し直してください。"
            )
    print("GPU依存とuv.lockの一致を確認しました。")


if __name__ == "__main__":
    main()
