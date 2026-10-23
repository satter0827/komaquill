"""Append safe operational events under a process-shared lock."""

import json
import sys
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from filelock import FileLock

from komaquill.domain.errors import FailureInfo
from komaquill.infrastructure.paths import WorkspacePaths
from komaquill.messages import message


def record_event(
    project: WorkspacePaths,
    operation: str,
    result: str,
    *,
    runtime_id: UUID | None = None,
    run_id: UUID | None = None,
    failure: FailureInfo | None = None,
    operation_id: UUID | None = None,
    elapsed_seconds: float | None = None,
    asset: str | None = None,
) -> bool:
    """Append an event; return false if diagnostics storage is unavailable."""
    folder = project.artifacts / "logs"
    if runtime_id is not None:
        folder /= str(runtime_id)
    path = folder / ("events.jsonl" if runtime_id is not None else "cli.jsonl")
    data = {
        "created_at": datetime.now(UTC).isoformat(),
        "operation": operation,
        "asset": asset,
        "operation_id": str(operation_id) if operation_id else None,
        "elapsed_seconds": elapsed_seconds,
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
                message("text.cannot_save_the_operation_log_check_free_space"),
                file=sys.stderr,
            )
        return False
