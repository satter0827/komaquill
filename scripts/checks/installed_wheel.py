"""Verify an explicitly selected wheel without using a GPU or provider account."""

import argparse
import hashlib
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from scripts._support import atomic_json, input_fingerprint, message, source_revision


def main(wheel: Path, report_dir: Path) -> None:
    """Invoke installed-wheel acceptance tests with live switches removed."""
    root = Path(__file__).resolve().parents[2]
    wheel = wheel.resolve()
    if not wheel.is_file() or not wheel.name.startswith("komaquill-") or wheel.suffix != ".whl":
        raise SystemExit(message("quality.select_one_wheel"))
    report_dir = report_dir / uuid4().hex
    report_dir.mkdir(parents=True, exist_ok=False)
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
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/e2e/test_wheel.py",
        "-q",
        f"--junitxml={report_dir / 'e2e.xml'}",
    ]
    record = {
        "format_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision(root),
        "input_sha256": input_fingerprint(root),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "status": "running",
        "exit_code": None,
    }
    atomic_json(report_dir / "run.json", record)
    try:
        subprocess.run(command, cwd=root, env=env, check=True)
        record.update(status="passed", exit_code=0)
    except subprocess.CalledProcessError as error:
        record.update(status="failed", exit_code=error.returncode)
        raise
    finally:
        if record["status"] == "running":
            record["status"] = "interrupted"
        record["finished_at"] = datetime.now(UTC).isoformat()
        atomic_json(report_dir / "run.json", record)


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
