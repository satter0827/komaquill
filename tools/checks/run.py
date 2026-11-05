"""Run affected monorepo checks and retain the scope of the evidence."""

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.reporting import atomic_json, comparison_base, input_fingerprint, policy, source_revision
from tools.runtime.processes import run_owned

ROOT = Path(__file__).resolve().parents[2]


def main(scope: str = "daily", *, plan: bool = False) -> bool:
    report = ROOT / ".artifacts/records/reports/checks" / uuid4().hex
    base = comparison_base()
    selection = ["run-many"] if scope == "all" else ["affected", f"--base={base}"]
    command = ["node", "tools/runtime/nx.mjs", *selection, "-t", "lint,typecheck,test,build"]
    if scope != "daily":
        command.append("--configuration=ci")
    if plan:
        command = [
            "node",
            "tools/runtime/nx.mjs",
            "show",
            "projects",
            *([] if scope == "all" else ["--affected", f"--base={base}"]),
        ]
        return subprocess.run(command, cwd=ROOT, check=False).returncode == 0
    report.mkdir(parents=True, exist_ok=True)
    fingerprint = input_fingerprint(ROOT)
    payload: dict[str, object] = {
        "scope": scope,
        "base": base,
        "command": command,
        "source_revision": source_revision(ROOT),
        "input_sha256": fingerprint,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "owner_pid": os.getpid(),
    }
    atomic_json(report / "checks.json", payload)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("KOMAQUILL_ALLOW_", "LANGCHAIN_", "LANGSMITH_"))
        and key != "PYTEST_ADDOPTS"
    }
    env.update(PYTHONUTF8="1", KOMAQUILL_PYTHON=sys.executable, NX_BASE=base)
    try:
        with (report / "output.log").open("w", encoding="utf-8") as output:
            limits = policy(ROOT)
            step_timeout = float(limits["step_timeout"])
            pipeline_timeout = float(limits["pipeline_timeout"])
            if not 0 < step_timeout < pipeline_timeout or not all(
                isfinite(value) for value in (step_timeout, pipeline_timeout)
            ):
                payload["failure_code"] = "invalid_quality_limits"
                raise ValueError
            payload["timeout_seconds"] = pipeline_timeout
            atomic_json(report / "checks.json", payload)
            result = run_owned(
                command,
                cwd=ROOT,
                env=env,
                stdout=output,
                timeout=pipeline_timeout,
            )
        success = result == 0 and input_fingerprint(ROOT) == fingerprint
        payload.update(status="passed" if success else "failed", exit_code=result)
    except subprocess.TimeoutExpired:
        success = False
        payload.update(status="timed_out", exit_code=None)
    except (OSError, subprocess.SubprocessError, RuntimeError, KeyboardInterrupt):
        success = False
        payload.update(status="interrupted", exit_code=None)
    except Exception as error:
        success = False
        payload.update(status="failed", exit_code=None, failure_type=type(error).__name__)
    try:
        output_text = (report / "output.log").read_text(encoding="utf-8", errors="replace")
        payload["output_available"] = True
    except OSError:
        output_text = ""
        payload["output_available"] = False
    payload["finished_at"] = datetime.now(UTC).isoformat()
    atomic_json(report / "checks.json", payload)
    if output_text:
        print(output_text)
    print(str(report / "checks.json"))
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("daily", "ci", "all"), default="daily")
    parser.add_argument("--plan", action="store_true")
    options = parser.parse_args()
    raise SystemExit(0 if main(options.scope, plan=options.plan) else 1)
