"""Verify an explicitly selected wheel without using a GPU or provider account."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main(wheel: Path, report_dir: Path) -> None:
    """Invoke installed-wheel acceptance tests with live switches removed."""
    root = Path(__file__).resolve().parents[1]
    wheel = wheel.resolve()
    if len(list(wheel.glob("komaquill-*-py3-none-any.whl"))) != 1:
        raise SystemExit("Select a directory containing exactly one Komaquill wheel.")
    report_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for name in list(env):
        if (name.startswith("KOMAQUILL_") and name != "KOMAQUILL_UV") or name == "PYTEST_ADDOPTS":
            env.pop(name)
    env["KOMAQUILL_WHEEL"] = str(wheel)
    env["PYTHONUTF8"] = "1"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_wheel.py",
            "-q",
            f"--junitxml={report_dir / 'e2e.xml'}",
        ],
        cwd=root,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check an installed wheel without a GPU.")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    options = parser.parse_args()
    try:
        main(options.wheel, options.report_dir)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
