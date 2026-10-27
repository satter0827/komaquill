"""Append safe operational events under a process-shared lock."""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from filelock import FileLock

from komaquill.domain.errors import FailureInfo
from komaquill.infrastructure.paths import WorkspacePaths
from komaquill.messages import message

if TYPE_CHECKING:
    from komaquill.domain.usage import AccountUsage

_OPERATION: ContextVar[UUID | None] = ContextVar("komaquill_operation", default=None)


@contextmanager
def operation_scope(operation_id: UUID | None = None) -> Iterator[UUID]:
    """Correlate nested operations without coupling them to a GPU session."""
    identity = operation_id or _OPERATION.get() or uuid4()
    token = _OPERATION.set(identity)
    try:
        yield identity
    finally:
        _OPERATION.reset(token)


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
    story_id: UUID | None = None,
    render_id: UUID | None = None,
    scene_id: UUID | None = None,
    phase: str | None = None,
    exit_code: int | None = None,
    sdk_version: str | None = None,
    function: str | None = None,
    byte_count: int | None = None,
    sha256: str | None = None,
    usage: "AccountUsage | None" = None,
) -> bool:
    """Append an event; return false if diagnostics storage is unavailable."""
    folder = project.logs
    if runtime_id is not None:
        folder /= str(runtime_id)
    path = folder / ("events.jsonl" if runtime_id is not None else "cli.jsonl")
    session_operation_id = None
    with suppress(OSError, ValueError, KeyError, TypeError):
        monitor = json.loads((project.state / "monitor.json").read_bytes())
        if (
            runtime_id is not None
            and monitor.get("runtime_id") == str(runtime_id)
            and monitor.get("phase") not in {"stopped", "failed"}
        ):
            session_operation_id = monitor["operation_id"]
    operation_id = operation_id or _OPERATION.get()
    if operation_id is None:
        with suppress(ValueError):
            operation_id = UUID(os.environ.get("KOMAQUILL_OPERATION_ID", ""))
    data = {
        "format_version": 2,
        "session_operation_id": session_operation_id,
        "created_at": datetime.now(UTC).isoformat(),
        "operation": operation,
        "asset": asset,
        "operation_id": str(operation_id) if operation_id else None,
        "elapsed_seconds": elapsed_seconds,
        "result": result,
        "runtime_id": str(runtime_id) if runtime_id else None,
        "run_id": str(run_id) if run_id else None,
        "story_id": str(story_id) if story_id else None,
        "render_id": str(render_id) if render_id else None,
        "scene_id": str(scene_id) if scene_id else None,
        "failure": failure.model_dump(mode="json") if failure else None,
        "phase": phase,
        "exit_code": exit_code,
        "sdk_version": sdk_version,
        "function": function,
        "byte_count": byte_count,
        "sha256": sha256,
        "usage": usage.model_dump(mode="json") if usage is not None else None,
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
