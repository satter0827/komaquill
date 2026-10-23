"""Inspect publishable files and history for secrets."""

import os
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from detect_secrets.core.scan import scan_diff, scan_file
from detect_secrets.settings import default_settings

from komaquill.storage import atomic_json


def git_text(root: Path, *arguments: str) -> str:
    """Read Git content without displaying it."""
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, encoding="utf-8", errors="replace"
    )


def main() -> None:
    """Inspect public files and history without persisting secret values."""
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    tracked = git_text(root, "ls-files", "-z").split("\0")
    forbidden = (".artifacts/", "artifacts/", ".venv/", ".env", "__pycache__/")
    if any(path.startswith(forbidden) and path != ".env.example" for path in tracked if path):
        raise SystemExit("生成物または資格情報が追跡されています。")
    candidates = git_text(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ).split("\0")
    findings: list[dict[str, str | int]] = []
    with default_settings() as settings:
        settings.disable_filters(
            "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
        )
        for name in candidates:
            if name and (root / name).is_file():
                findings.extend(
                    {"file": secret.filename, "line": secret.line_number, "type": secret.type}
                    for secret in scan_file(name)
                )
        for diff in [
            git_text(root, "diff", "--cached", "--no-ext-diff"),
            git_text(root, "log", "--all", "--format=", "--patch", "--no-ext-diff"),
        ]:
            if diff.strip():
                findings.extend(
                    {"file": secret.filename, "line": secret.line_number, "type": secret.type}
                    for secret in scan_diff(diff)
                )
    report = root / ".artifacts/reports/quality/security.json"
    atomic_json(
        report,
        {
            "created_at": datetime.now(UTC).isoformat(),
            "source_revision": git_text(root, "rev-parse", "HEAD").strip(),
            "tool": "detect-secrets",
            "tool_version": version("detect-secrets"),
            "scope": ["working_tree", "staged", "history"],
            "findings": findings,
        },
    )
    if findings:
        for item in findings:
            print(f"確認が必要です: {item['file']}:{item['line']} ({item['type']})")
        raise SystemExit(1)
    print("秘密情報と追跡対象を確認しました。")


if __name__ == "__main__":
    main()
