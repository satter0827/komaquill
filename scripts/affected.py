"""Select related Python tests from the checked-in import graph without product imports."""

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._support import comparison_base, message

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOTS = ("tests/unit", "tests/integration", "tests/structure")
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
    forced: set[str] = set()
    for name in changes:
        if name.startswith("tests/") and name.endswith(("/conftest.py", "/__init__.py")):
            folder = name.rsplit("/", 1)[0] + "/"
            forced.update(path for path in tests if path.startswith(folder))
    if "scripts/messages.json" in changes:
        forced.add("tests/unit/test_messages.py")
    if any(name.startswith("bootstrap/") and name.endswith(".ps1") for name in changes):
        forced.add("tests/integration/test_distribution.py")
    if any(name.startswith(".vscode/") and name.endswith(".json") for name in changes):
        forced.add("tests/structure/test_editor.py")
    if changes & SHARED or any(
        name.startswith("src/backend/src/komaquill/resources/")
        or name.startswith("tests/fixtures/")
        for name in changes
    ):
        return tests
    files = [
        path
        for folder in ("src/backend/src", "tests", "scripts", "bootstrap")
        for path in (root / folder).rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    nodes = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        module = (
            relative.removeprefix("src/backend/src/")
            .removesuffix(".py")
            .replace("/", ".")
            .removesuffix(".__init__")
        )
        nodes[module] = relative
    dependencies: dict[str, set[str]] = {}
    for module, relative in nodes.items():
        imports: set[str] = set()
        try:
            tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError):
            return tests
        import_functions = {"__import__", "import_module"} | {
            alias.asname or alias.name
            for item in ast.walk(tree)
            if isinstance(item, ast.ImportFrom) and item.module == "importlib"
            for alias in item.names
            if alias.name == "import_module"
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(name.name for name in node.names)
            elif isinstance(node, ast.ImportFrom):
                package = module if relative.endswith("/__init__.py") else module.rpartition(".")[0]
                prefix = (
                    ".".join(package.split(".")[: len(package.split(".")) - node.level + 1])
                    if node.level
                    else ""
                )
                target = ".".join(part for part in (prefix, node.module) if part)
                imports.add(target)
                imports.update(target + "." + name.name for name in node.names)
            elif isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
                or (isinstance(node.func, ast.Name) and node.func.id in import_functions)
            ):
                if (
                    not node.args
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                    or node.args[0].value.startswith(".")
                ):
                    return tests
                imports.add(node.args[0].value)
        dependencies[module] = imports | {
            name.rsplit(".", count)[0]
            for name in imports
            for count in range(1, len(name.split(".")))
            if name.rsplit(".", count)[0] in nodes
        }
    impacted = {module for module, path in nodes.items() if path in changes}
    impacted.update(
        name.removeprefix("src/backend/src/")
        .removesuffix(".py")
        .replace("/", ".")
        .removesuffix(".__init__")
        for name in changes
        if name.endswith(".py")
    )
    while True:
        following = impacted | {
            module for module, imports in dependencies.items() if imports & impacted
        }
        if following == impacted:
            break
        impacted = following
    return [
        path
        for path in tests
        if path in changes or path in forced or any(nodes.get(name) == path for name in impacted)
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
                        "scope": "backend-all" if args.all else "backend-related",
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
        "scope": "backend-all" if args.all else "backend-related",
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
    env["COVERAGE_FILE"] = str(record.with_suffix(".coverage"))
    try:
        result = (
            subprocess.run(
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
            ).returncode
            if selected
            else 0
        )
        if result == 0 and args.coverage:
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "scripts.checks.coverage",
                    "--report",
                    str(record.with_suffix(".coverage.json")),
                ],
                cwd=ROOT,
                env=env,
            ).returncode
        payload.update(status="passed" if result == 0 else "failed", exit_code=result)
    except (OSError, KeyboardInterrupt):
        result = 1
        payload.update(status="interrupted", exit_code=result)
    payload["finished_at"] = datetime.now(UTC).isoformat()
    record.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    raise SystemExit(result)


if __name__ == "__main__":
    main()
