"""Run a detached session supervisor with frozen settings and safe output."""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import keyring
import psutil
from filelock import FileLock

from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.domain.models import Session
from komaquill.infrastructure import runtime
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.monitor_state import read_monitor, update_monitor, write_monitor
from komaquill.infrastructure.views import open_view
from komaquill.messages import error_message


def main(root: Path) -> int:
    """Own the process record until provider shutdown and local cleanup finish."""
    project = Project(root.resolve())
    state = read_monitor(project)
    if state is None:
        return 1
    with FileLock(str(project.state / ".monitor-state.lock")):
        state = read_monitor(project)
        if state is None:
            return 1
        state.pid = os.getpid()
        state.process_started_at = psutil.Process().create_time()
        state.observed_at = datetime.now(UTC)
        write_monitor(project, state)
    project = project.configured(state.settings)
    record_event(project, "monitor", "started", operation_id=state.operation_id)
    password = None
    try:
        if state.username is not None and state.endpoint is not None:
            session = Session(
                endpoint=state.endpoint,
                backend="external",
                runtime_id=state.operation_id,
                username=state.username,
            )
            password = keyring.get_password("komaquill", runtime.credential_key(project, session))
            if password is None:
                raise KomaquillError(code="authentication_failed")

        def ready(session: Session) -> None:
            opened = open_view(project, session, strict=False) if state.open_browser else None
            update_monitor(
                project, phase="ready", runtime_id=session.runtime_id, browser_opened=opened
            )

        runtime.start(
            project, state.backend, state.endpoint, state.username, password, print, ready
        )
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, KomaquillError) and error.code == "stopping":
            update_monitor(project, phase="stopped")
            return 0
        failure = failure_info(error, "operation")
        record_event(project, "monitor", "failed", operation_id=state.operation_id, failure=failure)
        update_monitor(project, phase="failed", failure=failure)
        if isinstance(error, KomaquillError):
            print(error_message(error), file=sys.stderr)
        return 1
    update_monitor(project, phase="stopped")
    record_event(project, "monitor", "stopped", operation_id=state.operation_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
