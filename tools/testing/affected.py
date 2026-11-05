"""Select related Python tests from the checked-in import graph without product imports."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psutil

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.reporting import comparison_base, message, policy
from tools.runtime.processes import run_owned

ROOT = Path(__file__).resolve().parents[2]
TEST_ROOTS = ("tests",)
SHARED = {
    "pyproject.toml",
    "src/backend/pyproject.toml",
    "src/backend/uv.lock",
    "tests/conftest.py",
    "tests/__init__.py",
}


def changed(root: Path, base: str) -> set[str]:
    common = subprocess.check_output(
        ["git", "merge-base", "HEAD", base], cwd=root, text=True
    ).strip()
    tracked = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", common], cwd=root, text=True
    )
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root, text=True
    )
    return set(tracked.split("\0") + untracked.split("\0")) - {""}


def related(root: Path, changes: set[str]) -> list[str]:
    tests = sorted(
        path.relative_to(root).as_posix()
        for folder in TEST_ROOTS
        for path in (root / folder).rglob("test_*.py")
    )
    forced = {path for path in tests if path in changes}
    for name in changes:
        if name.startswith("tests/") and name.endswith(("/conftest.py", "/__init__.py")):
            folder = name.rsplit("/", 1)[0] + "/"
            forced.update(path for path in tests if path.startswith(folder))
    if "scripts/resources/messages.json" in changes:
        forced.update(path for path in tests if path.endswith("/test_messages.py"))
    if any(name.startswith("bootstrap/") and name.endswith(".ps1") for name in changes):
        forced.add("tests/backend/integration/platform/test_distribution.py")
    if any(name.startswith(".vscode/") and name.endswith(".json") for name in changes):
        forced.add("tests/system/structure/editor/test_editor.py")
    if changes & SHARED or any(
        name.startswith(("src/backend/src/komaquill/resources/", "tests/fixtures/"))
        for name in changes
    ):
        return tests
    if not any(name.endswith(".py") for name in changes):
        return sorted(forced)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTEST_ADDOPTS"
        and not key.startswith(
            ("KOMAQUILL_ALLOW_", "KOMAQUILL_LIVE", "LANGCHAIN_", "LANGSMITH_", "OPENAI_")
        )
    }
    try:
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).with_name("dependencies.py"))],
            cwd=root,
            env=environment,
            input=json.dumps({"root": str(root), "changes": sorted(changes)}),
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        modules = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return tests
    if modules is None:
        return tests
    return [
        path
        for path in tests
        if path in forced or path.removesuffix(".py").replace("/", ".") in modules
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--base", default=comparison_base())
    parser.add_argument("--selection-hash", action="store_true")
    args = parser.parse_args()
    if args.coverage and not args.all:
        parser.error("--coverage requires --all")
    record = ROOT / ".artifacts/records/reports/affected" / (uuid4().hex + ".json")
    try:
        changes = changed(ROOT, args.base)
    except (OSError, subprocess.SubprocessError) as error:
        if not args.plan and not args.selection_hash:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(
                json.dumps(
                    {
                        "scope": "python-all" if args.all else "python-related",
                        "base": args.base,
                        "status": "failed",
                        "failure_type": type(error).__name__,
                    }
                ),
                encoding="utf-8",
            )
        raise SystemExit(message("quality.selection_failed")) from None
    selected = list(TEST_ROOTS) if args.all else related(ROOT, changes)
    selection_sha256 = hashlib.sha256(json.dumps(selected).encode()).hexdigest()
    if args.selection_hash:
        print(selection_sha256)
        return
    payload: dict[str, object] = {
        "scope": "python-all" if args.all else "python-related",
        "base": args.base,
        "changes": sorted(changes),
        "selected": selected,
        "selection_sha256": selection_sha256,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "planned",
    }
    if args.plan:
        print(json.dumps(payload, indent=2))
        return
    record.parent.mkdir(parents=True, exist_ok=True)
    payload.update(
        status="running", owner_pid=os.getpid(), owner_started=psutil.Process().create_time()
    )
    record.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LANGCHAIN_", "LANGSMITH_", "KOMAQUILL_ALLOW_", "KOMAQUILL_LIVE"))
        and key != "PYTEST_ADDOPTS"
    }
    extra = (
        [
            "--cov",
            "--cov-branch",
            "--cov-report=term:skip-covered",
            f"--cov-report=json:{record.with_suffix('.coverage.json')}",
        ]
        if args.coverage
        else []
    )
    if (ROOT / "tests/system/e2e/package/test_installed_wheel.py").is_file():
        wheels = list((ROOT / ".artifacts/dist").glob("komaquill-*.whl"))
        if len(wheels) != 1:
            payload.update(status="blocked", failure_code="wheel_unavailable")
            record.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            raise SystemExit(message("quality.select_one_wheel"))
        env["KOMAQUILL_WHEEL"] = str(wheels[0].resolve())
        env["KOMAQUILL_ACCEPTANCE_REPORT_DIR"] = str(record.parent / record.stem / "package")
    env["COVERAGE_FILE"] = str(record.with_suffix(".coverage"))
    env["PYTHONUNBUFFERED"] = "1"
    try:
        result = (
            run_owned(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pytest",
                    *selected,
                    "-q",
                    f"--junitxml={record.with_suffix('.xml')}",
                    *extra,
                ],
                cwd=ROOT,
                env=env,
                stdout=sys.stdout,
                timeout=float(policy(ROOT).get("step_timeout", 900)),
            )
            if selected
            else 0
        )
        if result == 0 and args.coverage:
            result = run_owned(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "tools.checks.coverage",
                    "--report",
                    str(record.with_suffix(".coverage.json")),
                ],
                cwd=ROOT,
                env=env,
                stdout=sys.stdout,
                timeout=60,
            )
        payload.update(status="passed" if result == 0 else "failed", exit_code=result)
    except subprocess.TimeoutExpired:
        result = 1
        payload.update(status="timed_out", exit_code=result)
    except (OSError, KeyboardInterrupt, RuntimeError):
        result = 1
        payload.update(status="interrupted", exit_code=result)
    payload["finished_at"] = datetime.now(UTC).isoformat()
    record.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    raise SystemExit(result)


if __name__ == "__main__":
    main()
