"""公開前とCIに共通する秘密情報・追跡対象の検査。"""

import json
import os
import subprocess
from pathlib import Path

from detect_secrets.core.scan import scan_diff, scan_file
from detect_secrets.settings import default_settings


def git_text(root: Path, *arguments: str) -> str:
    """Gitの内容を画面へ出さず検査へ渡す。"""
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, encoding="utf-8", errors="replace"
    )


def main() -> None:
    """公開対象と全履歴を検査し、秘密そのものを記録しない。"""
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    tracked = git_text(root, "ls-files", "-z").split("\0")
    forbidden = ("artifacts/", ".venv/", ".env", "__pycache__/")
    if any(path.startswith(forbidden) and path != ".env.example" for path in tracked if path):
        raise SystemExit("生成物または資格情報が追跡されています。")
    candidates = git_text(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ).split("\0")
    for name in candidates:
        if name.endswith(".ipynb"):
            notebook = json.loads((root / name).read_text(encoding="utf-8"))
            if any(
                cell.get("outputs") or cell.get("execution_count") is not None
                for cell in notebook["cells"]
            ):
                raise SystemExit("ノートブックの出力を消してから公開してください。")
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
    report = root / "artifacts/security/results.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    if findings:
        for item in findings:
            print(f"確認が必要です: {item['file']}:{item['line']} ({item['type']})")
        raise SystemExit(1)
    print("秘密情報と追跡対象を確認しました。")


if __name__ == "__main__":
    main()
