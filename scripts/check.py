"""Run the same GPU-free quality and security gates locally and in CI."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from komaquill.infrastructure.provenance import code_sha256, source_revision
from komaquill.infrastructure.storage import atomic_json
from komaquill.messages import message


def input_fingerprint(root: Path) -> str:
    """Identify every source and configuration input without hashing generated data."""
    digest = hashlib.sha256()
    roots = ("src", "scripts", "tests", "bootstrap", "docs", ".vscode", ".github", ".githooks")
    paths = [p for name in roots for p in (root / name).rglob("*") if p.is_file()]
    paths.extend(p for p in root.iterdir() if p.is_file() and p.name != ".DS_Store")
    for path in sorted(set(paths)):
        if "__pycache__" in path.parts or path.suffix == ".pyc" or path.is_symlink():
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main(group: str = "all") -> None:
    """Run selected gates, preserving a report even if a command fails."""
    root = Path(__file__).resolve().parents[1]
    report = root / ".artifacts/records/reports/quality" / uuid4().hex
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
        commands.append([python, str(root / "scripts/security.py"), "--report-dir", str(report)])
    results: list[dict[str, str | int]] = []
    started_at = datetime.now(UTC).isoformat()
    tested_code = code_sha256()
    tested_revision = source_revision()
    tested_inputs = input_fingerprint(root)
    completed = False
    primary: BaseException | None = None
    step_started = started_at
    active: list[str] | None = None
    try:
        for command in commands:
            active = command
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
            active = None
        if code_sha256() != tested_code or input_fingerprint(root) != tested_inputs:
            results.append(
                {
                    "step": "source_identity",
                    "exit_code": 1,
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
            raise SystemExit(message("quality.source_changed"))
        completed = len(results) == len(commands) and bool(commands)
    except BaseException as error:
        primary = error
        if active is not None:
            results.append(
                {
                    "step": " ".join(active[1:4]),
                    "exit_code": error.returncode
                    if isinstance(error, subprocess.CalledProcessError)
                    else 130
                    if isinstance(error, KeyboardInterrupt)
                    else 1,
                    "exception_type": type(error).__name__,
                    "started_at": step_started,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
        raise
    finally:
        data = {
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "group": group,
            "status": "passed"
            if completed
            else "interrupted"
            if isinstance(primary, KeyboardInterrupt)
            else "failed",
            "planned_steps": len(commands),
            "source_revision": tested_revision,
            "code_sha256": tested_code,
            "input_sha256": tested_inputs,
            "steps": results,
        }
        try:
            atomic_json(report / "gate.json", data)
            if completed:
                atomic_json(
                    report.parent / f"latest-{group}.json",
                    {
                        "report": report.name,
                        "source_revision": tested_revision,
                        "code_sha256": tested_code,
                        "input_sha256": tested_inputs,
                    },
                )
        except OSError:
            if primary is None:
                raise
            print(message("quality.report_unavailable"), file=sys.stderr)
        print(message("quality.check_report", path=report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=message("quality.run_project_checks"))
    parser.add_argument("--group", choices=["all", "quality", "security"], default="all")
    try:
        main(parser.parse_args().group)
    except (subprocess.CalledProcessError, OSError) as error:
        print(message("quality.project_checks_failed"), file=sys.stderr)
        raise SystemExit(
            error.returncode if isinstance(error, subprocess.CalledProcessError) else 1
        ) from None
