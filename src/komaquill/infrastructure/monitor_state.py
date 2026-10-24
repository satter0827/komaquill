"""Store supervisor state and distinguish live processes from stale records."""

import json
import os
from datetime import UTC, datetime
from uuid import UUID

import psutil
from filelock import FileLock

from komaquill.domain.errors import KomaquillError
from komaquill.domain.monitor import MonitorState
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.paths import WorkspacePaths

PID_TIME_TOLERANCE = 0.01
TERMINAL_PHASES = frozenset({"stopped", "failed"})


def read_monitor(project: WorkspacePaths) -> MonitorState | None:
    """Read a snapshot without creating files or repairing its contents."""
    path = project.state / "monitor.json"
    return MonitorState.model_validate_json(path.read_bytes()) if path.is_file() else None


def alive(state: MonitorState | None) -> bool:
    """Require a matching process creation time and an expected module."""
    if state is None or state.pid <= 0 or state.phase in TERMINAL_PHASES:
        return False
    try:
        process = psutil.Process(state.pid)
        args = process.cmdline()
        return (
            abs(process.create_time() - state.process_started_at) < PID_TIME_TOLERANCE
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
            and ("komaquill.interfaces.session_worker" in args or "--foreground" in args)
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def write_monitor(project: WorkspacePaths, state: MonitorState) -> None:
    """Write a validated snapshot atomically."""
    atomic_json(project.state / "monitor.json", state.model_dump(mode="json"))


def update_monitor(project: WorkspacePaths, **changes: object) -> None:
    """Update only the snapshot belonging to this exact process."""
    path = project.state / "monitor.json"
    if not path.is_file():
        return
    with FileLock(str(project.state / ".monitor-state.lock")):
        state = read_monitor(project)
        if state is None or state.pid != os.getpid():
            return
        if changes.get("phase", state.phase) != state.phase:
            changes["phase_started_at"] = datetime.now(UTC)
        data = {**state.model_dump(), **changes, "observed_at": datetime.now(UTC)}
        write_monitor(project, MonitorState.model_validate(data))


def cancel_requested(project: WorkspacePaths) -> bool | None:
    """Match cancellation to a startup operation rather than an old connection."""
    path = project.state / "monitor-stop.json"
    state = read_monitor(project)
    if state is None or not path.is_file():
        return None
    data = json.loads(path.read_bytes())
    return bool(data["force"]) if data.get("operation_id") == str(state.operation_id) else None


def request_cancel(project: WorkspacePaths, operation_id: UUID, force: bool) -> None:
    """Keep cancellation monotonic across simultaneous stop commands."""
    with FileLock(str(project.state / ".monitor-stop.lock")):
        state = read_monitor(project)
        if state is None or state.operation_id != operation_id:
            raise KomaquillError(code="identity_mismatch")
        force = force or cancel_requested(project) is True
        atomic_json(
            project.state / "monitor-stop.json",
            {
                "operation_id": str(operation_id),
                "force": force,
            },
        )


def check_cancel(project: WorkspacePaths) -> None:
    """Abort preparation before the next allocating or expensive operation."""
    if cancel_requested(project) is not None:
        raise KomaquillError(code="stopping", stage="preparation", retryable=False)
