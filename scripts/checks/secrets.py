"""Inspect publishable files and history for secrets."""

import argparse
import os
import re
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from detect_secrets.core.scan import scan_diff, scan_file
from detect_secrets.settings import default_settings

from scripts._support import atomic_json, message


def is_public_package_integrity(filename: str, line: str) -> bool:
    """Exclude only canonical pnpm integrity fields, keeping other lockfile text scanned."""
    return (
        filename == "pnpm-lock.yaml"
        and re.fullmatch(
            r"\s*(?:resolution: \{integrity: sha512-[A-Za-z0-9+/]{86}==\}"
            r'|integrity: "sha512-[A-Za-z0-9+/]{86}==")\s*',
            line,
        )
        is not None
    )


def git_text(root: Path, *arguments: str) -> str:
    """Read Git content without displaying it."""
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, encoding="utf-8", errors="replace"
    )


def main(report_dir: Path | None = None, *, scope: str = "history") -> None:
    """Inspect public files and history without persisting secret values."""
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    tracked = git_text(root, "ls-files", "-z").split("\0")
    forbidden = (".artifacts/", "artifacts/", ".venv/", ".env", "__pycache__/")
    if any(path.startswith(forbidden) and path != ".env.example" for path in tracked if path):
        raise SystemExit(message("quality.generated_data_or_credential_files_are_tracked_by"))
    candidates = git_text(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ).split("\0")
    findings: list[dict[str, str | int]] = []
    with default_settings() as settings:
        settings.configure_filters(
            [
                *({"path": path, **config} for path, config in settings.filters.items()),
                {"path": "scripts.checks.secrets.is_public_package_integrity"},
            ]
        )
        settings.disable_filters(
            "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
        )
        for name in candidates:
            if name and (root / name).is_file():
                findings.extend(
                    {"file": secret.filename, "line": secret.line_number, "type": secret.type}
                    for secret in scan_file(name)
                )
        differences = [
            git_text(root, "diff", "--cached", "--no-ext-diff"),
        ]
        if scope == "history":
            differences.append(
                git_text(root, "log", "--all", "--format=", "--patch", "--no-ext-diff")
            )
        for diff in differences:
            if diff.strip():
                findings.extend(
                    {"file": secret.filename, "line": secret.line_number, "type": secret.type}
                    for secret in scan_diff(diff)
                )
    report = (
        report_dir or root / ".artifacts/records/reports/quality" / uuid4().hex
    ) / f"security-{scope}.json"
    atomic_json(
        report,
        {
            "created_at": datetime.now(UTC).isoformat(),
            "source_revision": git_text(root, "rev-parse", "HEAD").strip(),
            "tool": "detect-secrets",
            "tool_version": version("detect-secrets"),
            "scope": ["working_tree", "staged"] + (["history"] if scope == "history" else []),
            "findings": findings,
        },
    )
    if findings:
        for item in findings:
            print(
                message(
                    "quality.scan_finding",
                    file=item["file"],
                    line=item["line"],
                    kind=item["type"],
                )
            )
        raise SystemExit(1)
    print(message("quality.scan_complete"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=message("quality.inspect_security"))
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--scope", choices=("worktree", "history"), default="history")
    options = parser.parse_args()
    main(options.report_dir, scope=options.scope)
