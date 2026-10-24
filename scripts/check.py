"""Run cumulative checks and preserve evidence independently of product imports."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# Direct script invocation puts scripts, rather than the repository, on sys.path.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._plan import inventory
from scripts._support import (
    atomic_json,
    freshness,
    input_fingerprint,
    message,
    policy,
    source_revision,
)


def main(
    level: int = 2,
    *,
    selected: list[str] | None = None,
    plan: bool = False,
    json_output: bool = False,
    status: bool = False,
) -> bool:
    """Run independent checks after failures without promoting partial verification."""
    if level not in {1, 2, 3}:
        raise ValueError(message("quality.invalid_level"))
    root = Path(__file__).resolve().parents[1]
    if status:
        data = freshness(root, level)
        print(
            json.dumps(data)
            if json_output
            else message("quality.evidence_status", data=json.dumps(data))
        )
        return True
    reports = root / ".artifacts/records/reports/quality"
    report = reports / uuid4().hex
    uv = os.environ.get("KOMAQUILL_UV") or shutil.which("uv") or "uv"
    checks = inventory(root, report, uv)
    if selected:
        known = {check.id for check in checks}
        if not set(selected) <= known:
            raise ValueError(message("quality.unknown_check"))
        identities = set(selected)
        for check in reversed(checks):
            if check.id in identities:
                identities.update(check.requires)
        checks = tuple(check for check in checks if check.id in identities)
    else:
        checks = tuple(check for check in checks if check.level <= level)
    if plan:
        data = {
            "scope": "checks" if selected else "level",
            "level": None if selected else level,
            "requirements": [
                "Python development environment",
                "Node.js for L2 and L3",
                "uv and Git",
            ],
            "report": str(report),
            "checks": [check.describe() for check in checks],
        }
        print(json.dumps(data) if json_output else "\n".join(check.id for check in checks))
        return True

    env = dict(os.environ)
    for name in list(env):
        if name.startswith("KOMAQUILL_") and name != "KOMAQUILL_UV":
            env.pop(name)
    env.pop("PYTEST_ADDOPTS", None)
    for name in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        env.pop(name, None)
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TEST_TRACKING"):
        env[name] = "false"
    env["PYTHONPYCACHEPREFIX"] = str(root / ".artifacts/cache/pycache")
    env["PYTHONUTF8"] = "1"
    env["COVERAGE_FILE"] = str(report / ".coverage")
    timeout = float(policy(root).get("step_timeout", 600))
    started = datetime.now(UTC).isoformat()
    fingerprint = input_fingerprint(root)
    revision = source_revision(root)
    steps: list[dict[str, object]] = []
    outcomes: dict[str, str] = {}
    interrupted = False
    report.mkdir(parents=True, exist_ok=True)
    for check in checks:
        step_started = datetime.now(UTC).isoformat()
        result: dict[str, object] = {
            "id": check.id,
            "started_at": step_started,
            "exit_code": None,
            "status": "skipped",
            "reason": None,
        }
        if interrupted or any(outcomes.get(key) != "passed" for key in check.requires):
            result["reason"] = "interrupted" if interrupted else "prerequisite_failed"
        else:
            try:
                if check.id == "build":
                    atomic_json(
                        root / ".artifacts/dist" / report.name / ".owner.json",
                        {
                            "owner": "quality",
                            "report": report.name,
                            "purpose": "distribution",
                        },
                    )
                subprocess.run(check.command, cwd=root, env=env, check=True, timeout=timeout)
                result.update(status="passed", exit_code=0)
            except KeyboardInterrupt:
                interrupted = True
                result.update(
                    status="interrupted", exit_code=130, exception_type="KeyboardInterrupt"
                )
            except (OSError, subprocess.SubprocessError) as error:
                result.update(
                    status="failed",
                    exit_code=error.returncode
                    if isinstance(error, subprocess.CalledProcessError)
                    else 1,
                    exception_type=type(error).__name__,
                    os_error=getattr(error, "winerror", None) or getattr(error, "errno", None),
                )
        result["finished_at"] = datetime.now(UTC).isoformat()
        steps.append(result)
        outcomes[check.id] = str(result["status"])
    unchanged = input_fingerprint(root) == fingerprint
    passed = unchanged and all(state == "passed" for state in outcomes.values()) and bool(checks)
    data = {
        "format_version": 2,
        "scope": "checks" if selected else "level",
        "level": None if selected else level,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "source_revision": revision,
        "input_sha256": fingerprint,
        "inputs_unchanged": unchanged,
        "status": "passed" if passed else "interrupted" if interrupted else "failed",
        "planned_steps": len(checks),
        "steps": steps,
    }
    pointer = {
        "report": report.name,
        "source_revision": revision,
        "input_sha256": fingerprint,
        "level": data["level"],
        "status": data["status"],
    }
    try:
        atomic_json(report / "gate.json", data)
        if not selected:
            atomic_json(reports / f"latest-attempt-l{level}.json", pointer)
            if passed:
                atomic_json(reports / f"latest-success-l{level}.json", pointer)
    except OSError:
        print(message("quality.report_unavailable"), file=sys.stderr)
        return False
    if json_output:
        print(json.dumps({**pointer, "report_path": str(report / "gate.json")}))
    else:
        print(message("quality.check_report", path=report))
    if interrupted:
        raise KeyboardInterrupt
    if not passed:
        print(message("quality.project_checks_failed"), file=sys.stderr)
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=message("quality.run_project_checks"))
    parser.add_argument("--level", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--check", action="append", dest="selected")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    try:
        raise SystemExit(0 if main(**vars(parser.parse_args())) else 1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError):
        print(message("quality.project_checks_failed"), file=sys.stderr)
        raise SystemExit(1) from None
