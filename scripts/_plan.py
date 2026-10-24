"""Declare the cumulative GPU-free check inventory."""

import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    """Identify a check and the successful prerequisites it needs."""

    id: str
    level: int
    command: tuple[str, ...]
    requires: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return asdict(self)


def inventory(root: Path, report: Path, uv: str) -> tuple[Check, ...]:
    """Use the same explicit checks for local runs and CI."""
    python = sys.executable

    def module(name: str, *args: str) -> tuple[str, ...]:
        return (python, "-m", f"scripts.checks.{name}", *args)

    dist = root / ".artifacts/dist" / report.name
    return (
        Check("format", 1, (python, "-m", "ruff", "format", "--check", ".")),
        Check("lint", 1, (python, "-m", "ruff", "check", ".")),
        Check("types", 1, (python, "-m", "mypy", "src", "scripts", "bootstrap")),
        Check(
            "imports",
            1,
            (
                python,
                "-c",
                "from importlinter.cli import lint_imports_command; lint_imports_command()",
                "--cache-dir",
                ".artifacts/cache/import-linter",
            ),
        ),
        Check("documentation", 1, module("documentation")),
        Check(
            "secrets-worktree",
            1,
            module("secrets", "--scope", "worktree", "--report-dir", str(report)),
        ),
        Check("javascript", 2, ("node", "--test", "tests/js/*.test.mjs")),
        Check("worker-lock", 2, module("worker_lock")),
        Check(
            "tests",
            2,
            (
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
            ),
        ),
        Check(
            "coverage", 2, module("coverage", "--report", str(report / "coverage.json")), ("tests",)
        ),
        Check("build", 3, (uv, "build", "--out-dir", str(dist))),
        Check("distribution", 3, module("distribution", "--dist-dir", str(dist)), ("build",)),
        Check(
            "installed-wheel",
            3,
            module("installed_wheel", "--dist-dir", str(dist), "--report-dir", str(report)),
            ("distribution",),
        ),
        Check(
            "secrets-history",
            3,
            module("secrets", "--scope", "history", "--report-dir", str(report)),
        ),
    )
