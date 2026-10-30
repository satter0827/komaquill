"""Verify an explicitly selected wheel without using a GPU or provider account."""

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from scripts._support import message


def main(wheel: Path, report_dir: Path) -> None:
    """Invoke installed-wheel acceptance tests with live switches removed."""
    root = Path(__file__).resolve().parents[2]
    wheel = wheel.resolve()
    if not wheel.is_file() or not wheel.name.startswith("komaquill-") or wheel.suffix != ".whl":
        raise SystemExit(message("quality.select_one_wheel"))
    report_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for name in list(env):
        if (
            (name.startswith("KOMAQUILL_") and name != "KOMAQUILL_UV")
            or name.startswith(("LANGCHAIN_", "LANGSMITH_"))
            or name == "PYTEST_ADDOPTS"
        ):
            env.pop(name)
    env["KOMAQUILL_WHEEL"] = str(wheel)
    env["KOMAQUILL_ACCEPTANCE_REPORT_DIR"] = str(report_dir / "failure-evidence")
    env["PYTHONUTF8"] = "1"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/e2e/test_wheel.py",
            "-q",
            f"--junitxml={report_dir / 'e2e.xml'}",
        ],
        cwd=root,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=message("quality.check_installed_wheel"))
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    version = tomllib.loads((root / "src/backend/pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    try:
        main(options.dist_dir / f"komaquill-{version}-py3-none-any.whl", options.report_dir)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
