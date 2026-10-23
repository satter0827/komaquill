"""Run the same GPU-free quality and security gates locally and in CI."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from komaquill.infrastructure.provenance import code_sha256, source_revision
from komaquill.messages import message


def main(group: str = "all") -> None:
    """Run selected gates, preserving a report even if a command fails."""
    root = Path(__file__).resolve().parents[1]
    report = root / ".artifacts/reports/quality" / uuid4().hex
    report.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("KOMAQUILL_") and name != "KOMAQUILL_UV":
            env.pop(name)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONPYCACHEPREFIX"] = str(root / ".artifacts/cache/pycache")
    env["PYTHONUTF8"] = "1"
    env["COVERAGE_FILE"] = str(report / ".coverage")
    python = sys.executable
    commands = []
    if group in {"all", "quality"}:
        uv = env.get("KOMAQUILL_UV") or shutil.which("uv")
        if not uv:
            raise SystemExit(
                message("quality.uv_is_unavailable_activate_the_development_environment")
            )
        dist = root / ".artifacts/dist" / report.name
        commands.extend(
            [
                [python, "-m", "ruff", "format", "--check", "."],
                [python, "-m", "ruff", "check", "."],
                [python, "-m", "mypy", "src", "scripts"],
                ["node", "--test", "tests/js/*.test.mjs"],
                [python, str(root / "scripts/check_worker_lock.py")],
                [
                    python,
                    "-c",
                    "from importlinter.cli import lint_imports_command; lint_imports_command()",
                    "--cache-dir",
                    ".artifacts/cache/import-linter",
                ],
                [
                    python,
                    "-m",
                    "pytest",
                    "--ignore=tests/e2e",
                    "--ignore=tests/live",
                    f"--junitxml={report / 'results.xml'}",
                    "--cov",
                    "--cov-branch",
                    "--cov-report=term-missing:skip-covered",
                    f"--cov-report=json:{report / 'coverage.json'}",
                    f"--cov-report=html:{report / 'coverage'}",
                ],
                [
                    python,
                    str(root / "scripts/check_coverage.py"),
                    "--report",
                    str(report / "coverage.json"),
                ],
                [uv, "build", "--out-dir", str(dist)],
                [python, str(root / "scripts/check_package.py"), "--dist-dir", str(dist)],
                [
                    python,
                    str(root / "scripts/check_e2e.py"),
                    "--wheel",
                    str(dist),
                    "--report-dir",
                    str(report),
                ],
            ]
        )
    if group in {"all", "security"}:
        commands.append([python, str(root / "scripts/security.py")])
    results: list[dict[str, str | int]] = []
    started_at = datetime.now(UTC).isoformat()
    try:
        for command in commands:
            step_started = datetime.now(UTC).isoformat()
            result = subprocess.run(command, cwd=root, env=env, check=True)
            results.append(
                {
                    "step": " ".join(command[1:4]),
                    "exit_code": result.returncode,
                    "started_at": step_started,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
    except subprocess.CalledProcessError as error:
        results.append({"step": " ".join(error.cmd[1:3]), "exit_code": error.returncode})
        raise
    finally:
        (report / "gate.json").write_text(
            json.dumps(
                {
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "group": group,
                    "source_revision": source_revision(),
                    "code_sha256": code_sha256(),
                    "steps": results,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if results and all(row["exit_code"] == 0 for row in results):
            from komaquill.infrastructure.storage import atomic_json

            atomic_json(
                root / ".artifacts/reports/quality" / f"latest-{group}.json",
                {
                    "report": report.name,
                    "source_revision": source_revision(),
                    "code_sha256": code_sha256(),
                },
            )
        print(message("quality.check_report", path=report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=message("quality.run_project_checks"))
    parser.add_argument("--group", choices=["all", "quality", "security"], default="all")
    try:
        main(parser.parse_args().group)
    except subprocess.CalledProcessError as error:
        print(message("quality.project_checks_failed"), file=sys.stderr)
        raise SystemExit(error.returncode) from None
