"""Launch one detached monitor and attach terminal commands to its state."""

import importlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import psutil
from filelock import FileLock

from komaquill.adapters.colab import Colab
from komaquill.adapters.diagnostics import record_event
from komaquill.adapters.monitor_state import (
    TERMINAL_PHASES,
    alive,
    read_monitor,
    request_cancel,
    write_monitor,
)
from komaquill.adapters.storage import load_session
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.configuration import remaining_seconds
from komaquill.domain.errors import KomaquillError
from komaquill.domain.monitor import MonitorState
from komaquill.messages import message
from komaquill.runtime.connection import session_project
from komaquill.runtime.views import open_view


def authenticate(project: RuntimeWorkspace) -> None:
    """Run the official interactive flow only in the requesting terminal."""
    colab = Colab(project)
    colab.command("version")
    try:
        colab.provider_call("identity")
    except KomaquillError:
        import logging

        get_credentials = importlib.import_module("colab_cli.auth").get_credentials

        logging.disable(logging.CRITICAL)
        try:
            get_credentials(str(Path.home() / ".colab-cli-oauth-config.json"))
            colab.provider_call("identity")
        except Exception:
            raise KomaquillError(code="authentication_failed", retryable=False) from None


def spawn(project: RuntimeWorkspace) -> subprocess.Popen[bytes]:
    """Detach standard streams and the console without installing a service."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEBUGPY_", "PYDEVD_"))}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", KOMAQUILL_PROJECT_DIR=str(project.root))
    log = project.logs / "monitor.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    with log.open("ab") as output:
        return subprocess.Popen(
            [sys.executable, "-B", "-m", "komaquill.runtime.worker", str(project.root)],
            cwd=project.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            close_fds=True,
            **options,
        )


def wait_ready(
    project: RuntimeWorkspace, operation_id: object, echo: Callable[[str], None]
) -> MonitorState:
    """Show phase changes while the detached worker owns the operation."""
    deadline = time.monotonic() + project.settings().preparation_timeout
    previous = None
    while True:
        try:
            state = read_monitor(project)
            if state is None or state.operation_id != operation_id:
                raise KomaquillError(code="identity_mismatch")
            current = (state.phase, state.asset)
            if current != previous:
                echo(message("monitor.progress", phase=state.phase, asset=state.asset or "-"))
                previous = current
            if state.phase == "ready":
                return state
            if state.phase in TERMINAL_PHASES:
                if state.failure:
                    raise KomaquillError(code=state.failure.code, stage=state.failure.stage)
                raise KomaquillError(code="stopping", retryable=False)
            if state.pid and not alive(state):
                record_event(project, "monitor", "lost", runtime_id=state.runtime_id)
                raise KomaquillError(display_key="monitor.lost", code="connection_failed")
            if state.backend == "colab":
                from komaquill.adapters.compute_budget import enforce

                try:
                    enforce(project, allocation_pending=state.runtime_id is None)
                except (KomaquillError, OSError, ValueError):
                    request_cancel(project, state.operation_id, False)
                    raise
            receipt = Colab(project).receipt() if state.backend == "colab" else None
            remaining = remaining_seconds(
                receipt.started_at if receipt else None,
                state.settings.submission_timeout,
                datetime.now(UTC),
            )
            if (remaining is not None and remaining <= 0) or (
                remaining is None and time.monotonic() >= deadline
            ):
                request_cancel(project, state.operation_id, False)
                raise KomaquillError(display_key="monitor.timeout", code="operation_failed")
            time.sleep(
                min(state.settings.poll_interval, remaining)
                if remaining is not None
                else state.settings.poll_interval
            )
        except KeyboardInterrupt:
            state = read_monitor(project)
            if state is not None:
                request_cancel(project, state.operation_id, False)
                wait_stopped(project, state, echo)
            raise KomaquillError(code="stopping", retryable=False) from None


def start(
    project: RuntimeWorkspace,
    backend: Literal["colab", "external"],
    endpoint: str | None,
    username: str | None,
    open_browser: bool,
    echo: Callable[[str], None],
    foreground: Callable[[Path], int] | None = None,
    *,
    interactive_auth: bool = True,
) -> None:
    """Reuse an identical active supervisor or start exactly one new worker."""
    project = project.configured()
    project.state.mkdir(parents=True, exist_ok=True)
    with FileLock(str(project.state / ".supervisor-start.lock")):
        prior = read_monitor(project)
        reused = alive(prior)
        if reused and prior is not None:
            if (prior.backend, prior.endpoint, prior.username, prior.settings) != (
                backend,
                endpoint,
                username,
                project.settings(),
            ):
                raise KomaquillError(
                    display_key="text.settings_differ_from_the_active_session_run_stop"
                )
            state = prior
        else:
            if prior is not None and prior.phase not in TERMINAL_PHASES:
                record_event(project, "monitor", "lost", runtime_id=prior.runtime_id)
            if backend == "colab":
                if interactive_auth:
                    authenticate(project)
                else:
                    Colab(project).provider_call("identity")
            state = MonitorState(
                operation_id=uuid4(),
                backend=backend,
                endpoint=endpoint,
                username=username,
                settings=project.settings(),
                open_browser=open_browser,
                pid=os.getpid() if foreground else 0,
                process_started_at=psutil.Process().create_time() if foreground else 0,
                created_at=datetime.now(UTC),
                observed_at=datetime.now(UTC),
            )
            write_monitor(project, state)
            if foreground is None:
                process = spawn(project)
                with FileLock(str(project.state / ".monitor-state.lock")):
                    current = read_monitor(project)
                    if current is not None and current.pid == 0:
                        current.pid = process.pid
                        current.process_started_at = psutil.Process(process.pid).create_time()
                        write_monitor(project, current)
    if foreground is not None and not reused:
        if foreground(project.root):
            raise KomaquillError(code="operation_failed")
        return
    ready = wait_ready(project, state.operation_id, echo)
    if open_browser and reused:
        open_view(project, load_session(project), echo=echo)
    if ready.browser_opened is False and open_browser:
        session = load_session(project)
        echo(message("monitor.browser_failed", url=session.endpoint))
        raise KomaquillError(code="browser_failed")
    echo(message("monitor.ready"))


def wait_stopped(
    project: RuntimeWorkspace, state: MonitorState, echo: Callable[[str], None]
) -> None:
    """Wait for cleanup and escalate a second interrupt without killing ownership records."""
    deadline = time.monotonic() + state.settings.shutdown_timeout + state.settings.control_timeout
    while True:
        try:
            current = read_monitor(project)
            if current is None or current.operation_id != state.operation_id:
                raise KomaquillError(code="identity_mismatch")
            if current.phase in TERMINAL_PHASES:
                if current.failure:
                    raise KomaquillError(code=current.failure.code, stage=current.failure.stage)
                return
            if current.pid and not alive(current):
                raise KomaquillError(display_key="monitor.lost", code="connection_failed")
            if time.monotonic() >= deadline:
                raise KomaquillError(display_key="text.shutdown_is_still_in_progress_check_status")
            time.sleep(state.settings.poll_interval)
        except KeyboardInterrupt:
            request_cancel(project, state.operation_id, True)
            echo(message("monitor.force_requested"))


def stop(project: RuntimeWorkspace, force: bool, echo: Callable[[str], None]) -> bool:
    """Return false when legacy ownership recovery must handle a missing monitor."""
    try:
        state = read_monitor(project)
    except (ValueError, OSError):
        return False
    if state is None or (state.pid and not alive(state)) or state.phase in TERMINAL_PHASES:
        return False
    request_cancel(project, state.operation_id, force)
    echo(message("text.stop_requested_waiting_for_generation_and_collection"))
    wait_stopped(project.configured(state.settings), state, echo)
    echo(message("text.stopped_2"))
    return True


def events(project: RuntimeWorkspace, follow: bool) -> Iterator[dict[str, object]]:
    """Read new JSON events without printing unknown keys or exception payloads."""
    offsets: dict[Path, int] = {}
    interval = session_project(project).settings().poll_interval if follow else 0.0
    while True:
        for path in sorted((project.logs).glob("**/*.jsonl")):
            if path.name not in {"cli.jsonl", "events.jsonl", "worker-events.jsonl"}:
                continue
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offsets.get(path, 0))
                for line in handle:
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict):
                            yield row
                    except ValueError:
                        continue
                offsets[path] = handle.tell()
        if not follow:
            break
        time.sleep(interval)
