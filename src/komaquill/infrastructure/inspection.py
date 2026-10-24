"""Observe providers and local records without allocating or changing a session."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from filelock import Timeout
from pydantic import BaseModel

from komaquill.application.generation import verify_runtime
from komaquill.domain.assets import Preparation
from komaquill.domain.errors import KomaquillError
from komaquill.domain.materials import Material
from komaquill.domain.status import SessionStatus
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.monitor_state import alive, read_monitor
from komaquill.infrastructure.sessions.connection import connected, monitor_lock, session_project
from komaquill.infrastructure.storage import load_runs, load_session


def pending_count[T: BaseModel](
    folder: Path,
    model: type[T],
    predicate: Callable[[T], bool],
    unreadable: list[str],
) -> int | None:
    """Report incomplete records instead of presenting them as no pending work."""
    count = 0
    failed = False
    for path in folder.glob("*.json"):
        try:
            count += predicate(model.model_validate_json(path.read_bytes()))
        except (OSError, ValueError, KomaquillError):
            unreadable.append(f"{folder.name}/{path.name}")
            failed = True
    return None if failed else count


def inspect(project: Project) -> SessionStatus:
    """Collect independent observations while preserving unreadable identities."""
    result = SessionStatus()
    try:
        project = session_project(project)
    except (OSError, ValueError, KomaquillError):
        result.unreadable.append("session.json")
    colab = Colab(project)
    receipt = None
    active = None
    try:
        receipt = colab.receipt()
        if receipt is not None:
            with colab.lock.acquire(timeout=0):
                active = colab.refresh(receipt)
            result.gpu = (
                "active"
                if active
                else ("unconfirmed" if receipt.endpoint_id is None else "stopped")
            )
    except Timeout:
        result.gpu = "busy"
    except (OSError, ValueError, KomaquillError):
        result.gpu = "unknown"
    lock = monitor_lock(project)
    try:
        with lock.acquire(timeout=0):
            pass
    except Timeout:
        result.collector_running = True
    session = None
    if project.session_file.is_file():
        try:
            session = load_session(project)
        except (OSError, ValueError, KomaquillError):
            result.unreadable.append("session.json")
            result.connection = "unknown"
    if session is not None and receipt is None and session.backend == "colab":
        result.connection = "stopped"
    elif receipt is not None and active is False:
        result.connection = "not_started" if receipt.endpoint_id is None else "stopped"
    elif receipt is not None and (session is None or session.runtime_id != receipt.runtime_id):
        result.connection = "preparing"
    elif session is not None:
        try:
            with connected(project) as (client, current):
                client.json("/system_stats")
                verify_runtime(client, current)
            result.connection = "connected"
        except KomaquillError:
            result.connection = "disconnected"
    try:
        monitor = read_monitor(project)
        if monitor is not None:
            result.monitor_phase = (
                monitor.phase
                if (alive(monitor) or monitor.phase in {"stopped", "failed"})
                else "unknown"
            )
            result.observed_at = monitor.observed_at
    except (OSError, ValueError, KomaquillError):
        result.unreadable.append("monitor.json")
    result.preparation_count = pending_count(
        project.state / "preparation",
        Preparation,
        lambda item: item.phase not in {"ready", "cancelled", "failed"},
        result.unreadable,
    )
    result.material_count = pending_count(
        project.state / "preservation",
        Material,
        lambda item: item.state == "pending",
        result.unreadable,
    )
    invalid: list[UUID | str] = []
    runs = load_runs(project, unreadable=invalid)
    result.unreadable.extend(str(item) for item in invalid)
    result.pending = [str(run.run_id) for run in runs if run.pending]
    result.lost_count = sum(run.state == "lost" for run in runs)
    result.unreadable = list(dict.fromkeys(result.unreadable))
    return result
