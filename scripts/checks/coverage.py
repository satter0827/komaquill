"""Enforce independent statement and branch thresholds from coverage.py JSON."""

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

from scripts._support import message


def check_totals(totals: dict[str, Any], line_minimum: float, branch_minimum: float) -> list[str]:
    """Compare unrounded counts; reject an empty or inconsistent measurement."""
    failures: list[str] = []
    for label, covered_key, total_key, minimum in (
        (message("quality.statement"), "covered_lines", "num_statements", line_minimum),
        (message("quality.branch"), "covered_branches", "num_branches", branch_minimum),
    ):
        covered, total = totals[covered_key], totals[total_key]
        if (
            not isinstance(total, int)
            or not isinstance(covered, int)
            or total <= 0
            or not 0 <= covered <= total
        ):
            raise ValueError(message("quality.coverage_counts_are_invalid"))
        percentage = covered * 100 / total
        print(
            message(
                "quality.coverage",
                label=label,
                percentage=f"{percentage:.2f}",
                minimum=f"{minimum:g}",
            )
        )
        if covered * 100 < minimum * total:
            failures.append(label)
    return failures


def main(report_path: Path | None = None) -> None:
    """Read the single quality policy and inspect both coverage and source inventory."""
    root = Path(__file__).resolve().parents[2]
    policy = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "komaquill"
    ]["coverage"]
    report = json.loads(
        (report_path or root / ".artifacts/records/reports/quality/coverage.json").read_bytes()
    )
    expected = {
        path.relative_to(root).as_posix()
        for base in ("src/backend/src", "scripts", "bootstrap")
        for path in (root / base).rglob("*.py")
    }
    measured = {Path(name).as_posix() for name in report["files"]}
    if expected != measured:
        raise SystemExit(message("quality.coverage_source_inventory_does_not_match_src_and"))
    if check_totals(report["totals"], policy["line_minimum"], policy["branch_minimum"]):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=message("quality.enforce_coverage"))
    parser.add_argument("--report", type=Path)
    main(parser.parse_args().report)
