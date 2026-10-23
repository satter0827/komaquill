"""ローカルとCIに共通する品質確認。"""

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    """同じPython環境で全検査を実行する。"""
    root = Path(__file__).resolve().parents[1]
    (root / ".artifacts/reports/quality").mkdir(parents=True, exist_ok=True)
    os.environ["PYTHONPYCACHEPREFIX"] = str(root / ".artifacts/cache/pycache")
    commands = [
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "src", "scripts"],
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command\nlint_imports_command()",
            "--cache-dir",
            ".artifacts/cache/import-linter",
        ],
        [
            sys.executable,
            "-m",
            "pytest",
            "--junitxml=.artifacts/reports/quality/results.xml",
            "--cov",
            "--cov-branch",
            "--cov-report=term-missing:skip-covered",
            "--cov-report=json:.artifacts/reports/quality/coverage.json",
            "--cov-report=html",
        ],
        [sys.executable, str(root / "scripts/check_coverage.py")],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print("品質検査に失敗しました。直前の検査結果を確認してください。", file=sys.stderr)
        raise SystemExit(error.returncode) from None
