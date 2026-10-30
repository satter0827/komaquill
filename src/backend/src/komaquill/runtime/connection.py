"""Implement session connection through explicit local adapters."""

from collections.abc import Iterator
from contextlib import contextmanager

import keyring
from filelock import FileLock

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.comfyui import ComfyClient
from komaquill.adapters.storage import load_session, stop_request
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Session
from komaquill.images.generation import (
    collection_lock,
)


def session_project(project: RuntimeWorkspace) -> RuntimeWorkspace:
    """Use saved settings for status and stop even if the source file is broken."""
    try:
        session = load_session(project)
    except (OSError, ValueError):
        return project
    return project.configured(session.settings) if session.settings is not None else project


def credential_key(project: RuntimeWorkspace, session: Session) -> str:
    """Scope credential storage to the workspace, endpoint and username."""
    return f"{project.root}|{session.endpoint}|{session.username}"


@contextmanager
def connected(project: RuntimeWorkspace) -> Iterator[tuple[ComfyClient, Session]]:
    """Use a saved endpoint without allocating a runtime."""
    if not project.session_file.is_file():
        raise KomaquillError(display_key="text.no_connection_is_configured_run_start")
    session = load_session(project)
    password = None
    if session.username is not None:
        password = keyring.get_password("komaquill", credential_key(project, session))
        if password is None:
            raise KomaquillError(
                display_key="text.credentials_are_unavailable_run_start_to_reconnect"
            )
    client = ComfyClient(
        session.endpoint,
        password,
        (session.settings or project.settings()).request_timeout,
        username=session.username,
    )
    try:
        yield client, session
    finally:
        client.close()


def monitor_lock(project: RuntimeWorkspace) -> FileLock:
    """Prevent concurrent startup and teardown."""
    project.state.mkdir(parents=True, exist_ok=True)
    return FileLock(str(project.state / ".monitor.lock"))


def excluded_history(client: ComfyClient) -> set[str]:
    """Exclude queue and history entries that precede the connection."""
    excluded = {str(row[1]) for row in client.queue()}
    excluded.update(client.history())
    return excluded


def request_stop(project: RuntimeWorkspace, session: Session, force: bool) -> None:
    """Keep forced shutdown requests monotonic across processes."""
    with collection_lock(project), FileLock(str(project.state / ".stop.lock")):
        force = force or stop_request(project, session) is True
        atomic_json(
            project.state / "stop.json", {"runtime_id": str(session.runtime_id), "force": force}
        )
