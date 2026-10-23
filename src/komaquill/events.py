"""Append safe operational events under a process-shared lock."""

import json
import sys
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from filelock import FileLock

from komaquill.errors import KomaquillError
from komaquill.models import FailureInfo
from komaquill.settings import Project


def failure_info(error: BaseException, stage: str) -> FailureInfo:
    """Keep allowlisted attributes, never exception text or local variables."""
    return FailureInfo(
        code=error.code if isinstance(error, KomaquillError) else "storage_failed",
        stage=stage,
        exception_type=type(error).__name__,
        http_status=error.http_status if isinstance(error, KomaquillError) else None,
        retryable=error.retryable if isinstance(error, KomaquillError) else True,
    )


def record_event(
    project: Project,
    operation: str,
    result: str,
    *,
    runtime_id: UUID | None = None,
    run_id: UUID | None = None,
    failure: FailureInfo | None = None,
) -> bool:
    """Append an event; return false if diagnostics storage is unavailable."""
    folder = project.artifacts / "logs"
    if runtime_id is not None:
        folder /= str(runtime_id)
    path = folder / ("events.jsonl" if runtime_id is not None else "cli.jsonl")
    data = {
        "created_at": datetime.now(UTC).isoformat(),
        "operation": operation,
        "result": result,
        "runtime_id": str(runtime_id) if runtime_id else None,
        "run_id": str(run_id) if run_id else None,
        "failure": failure.model_dump(mode="json") if failure else None,
    }
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock"), path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False) + "\n")
            handle.flush()
        return True
    except OSError:
        with suppress(OSError):
            print(
                "操作ログを保存できません。保存先の空き容量とアクセス権を確認してください。",
                file=sys.stderr,
            )
        return False
