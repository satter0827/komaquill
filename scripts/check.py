"""Run affected monorepo checks and retain the scope of the evidence."""

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._support import atomic_json, comparison_base, input_fingerprint, source_revision

ROOT = Path(__file__).resolve().parents[1]


def main(scope: str = "daily", *, plan: bool = False) -> bool:
    report = ROOT / ".artifacts/records/reports/checks" / uuid4().hex
    base = comparison_base()
    selection = ["run-many"] if scope == "all" else ["affected", f"--base={base}"]
    command = ["node", "scripts/nx.mjs", *selection, "-t", "lint,typecheck,test,build"]
    if scope != "daily":
        command.append("--configuration=ci")
    if plan:
        command = [
            "node",
            "scripts/nx.mjs",
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
            result = subprocess.run(
                command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, check=False
            ).returncode
        print((report / "output.log").read_text(encoding="utf-8"))
        success = result == 0 and input_fingerprint(ROOT) == fingerprint
        payload.update(status="passed" if success else "failed", exit_code=result)
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
        success = False
        payload.update(status="interrupted", exit_code=None)
    payload["finished_at"] = datetime.now(UTC).isoformat()
    atomic_json(report / "checks.json", payload)
    print(str(report / "checks.json"))
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("daily", "ci", "all"), default="daily")
    parser.add_argument("--plan", action="store_true")
    options = parser.parse_args()
    raise SystemExit(0 if main(options.scope, plan=options.plan) else 1)
