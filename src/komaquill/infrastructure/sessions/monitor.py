"""Implement session monitor through explicit local adapters."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from komaquill.application.generation import (
    Collection,
    collect,
)
from komaquill.domain.configuration import remaining_seconds
from komaquill.domain.errors import KomaquillError, RuntimeEnded, failure_info
from komaquill.domain.models import Session
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.materials import MaterialCollector
from komaquill.infrastructure.monitor_state import (
    cancel_requested,
    update_monitor,
)
from komaquill.infrastructure.preparation import Coordinator, capabilities
from komaquill.infrastructure.sessions.connection import request_stop
from komaquill.infrastructure.storage import load_runs, stop_request
from komaquill.infrastructure.stories.publication import PublicationTracker
from komaquill.messages import message


def drain(
    project: Project,
    client: ComfyClient,
    session: Session,
    force: bool,
    echo: Callable[[str], None],
) -> Collection:
    """Wait within the startup snapshot and return collection outcomes explicitly."""
    project = project.configured(session.settings)
    settings = project.settings()
    deadline = time.monotonic() + settings.shutdown_timeout
    remaining = remaining_seconds(session.started_at, settings.session_timeout, datetime.now(UTC))
    if remaining is not None:
        deadline = min(deadline, time.monotonic() + max(0.0, remaining - settings.control_timeout))
    result = Collection(0, 0, 0)
    saved = 0
    publications = PublicationTracker(project)
    publications.reconcile()
    previous_errors: tuple[UUID | str, ...] = ()
    while True:
        try:
            if cancel_requested(project) is True:
                force = True
                request_stop(project, session, True)
            try:
                result = collect(project, client, session)
                publications.tick(result.observed_versions)
                material_errors = preserve(project, client) or ()
                if material_errors:
                    result = Collection(
                        result.saved,
                        result.pending,
                        result.failed,
                        (*result.errors, *material_errors),
                        result.pending_ids,
                        observed_versions=result.observed_versions,
                    )
                saved += result.saved
                if result.saved:
                    echo(message("text.saved_saved", saved=result.saved))
                if result.errors and result.errors != previous_errors:
                    echo(message("text.retry_collection_for") + ", ".join(map(str, result.errors)))
                previous_errors = result.errors
            except KomaquillError as error:
                unreadable: list[UUID | str] = []
                pending = tuple(
                    run.run_id
                    for run in load_runs(project, unreadable=unreadable)
                    if run.runtime_id == session.runtime_id and run.pending
                )
                echo(message("text.collection_could_not_finish_because_the_connection_is"))
                return Collection(
                    saved,
                    len(pending),
                    0,
                    tuple(unreadable),
                    pending,
                    failure_info(error, "collection"),
                )
            if (
                force
                or stop_request(project, session) is True
                or not (result.pending_ids or material_errors)
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(settings.poll_interval)
        except KeyboardInterrupt:
            force = True
            request_stop(project, session, True)
    if result.pending_ids:
        echo(message("text.pending_results") + ", ".join(map(str, result.pending_ids)))
    publications.reconcile()
    return Collection(
        saved,
        result.pending,
        result.failed,
        result.errors,
        result.pending_ids,
        observed_versions=result.observed_versions,
    )


def watch(
    project: Project,
    client: ComfyClient,
    session: Session,
    echo: Callable[[str], None],
    reconnect: Callable[[], None] | None = None,
    coordinator: Coordinator | None = None,
) -> bool:
    """Maintain the connection and collection until shutdown is requested."""
    project = project.configured(session.settings)
    disconnected = False
    previous_errors: tuple[UUID | str, ...] = ()
    sidecar_failures: dict[str, object] = {}
    publications = PublicationTracker(project)
    publications.reconcile()
    try:
        while stop_request(project, session) is None:
            if session.backend == "colab":
                from komaquill.infrastructure.compute_budget import enforce

                try:
                    enforce(project)
                except (KomaquillError, OSError, ValueError) as error:
                    record_event(
                        project,
                        "compute_budget",
                        "stopped",
                        runtime_id=session.runtime_id,
                        failure=failure_info(error, "colab_control"),
                    )
                    request_stop(project, session, False)
                    break
            remaining = remaining_seconds(
                session.started_at, project.settings().submission_timeout, datetime.now(UTC)
            )
            if remaining is not None and remaining <= 0:
                request_stop(project, session, False)
                break
            if (cancel := cancel_requested(project)) is not None:
                request_stop(project, session, cancel)
                break
            update_monitor(project)
            try:
                if reconnect is not None:
                    reconnect()
                if coordinator is not None:
                    try:
                        coordinator.tick()
                        sidecar_failures.pop("preparation", None)
                    except (KomaquillError, OSError, ValueError) as error:
                        failure = failure_info(error, "preparation")
                        if sidecar_failures.get("preparation") != failure:
                            record_event(
                                project,
                                "preparation",
                                "failed",
                                runtime_id=session.runtime_id,
                                failure=failure,
                            )
                            sidecar_failures["preparation"] = failure
                result = collect(project, client, session)
                publications.tick(result.observed_versions)
                if coordinator is not None:
                    try:
                        coordinator.materials.collect()
                        coordinator.materials.notify_runs()
                        sidecar_failures.pop("preservation", None)
                    except (KomaquillError, OSError, ValueError) as error:
                        failure = failure_info(error, "storage")
                        if sidecar_failures.get("preservation") != failure:
                            record_event(
                                project,
                                "preservation",
                                "failed",
                                runtime_id=session.runtime_id,
                                failure=failure,
                            )
                            sidecar_failures["preservation"] = failure
                if disconnected:
                    echo(message("text.reconnected"))
                    record_event(project, "connection", "restored", runtime_id=session.runtime_id)
                if result.saved:
                    echo(message("text.saved_saved", saved=result.saved))
                if result.errors and result.errors != previous_errors:
                    echo(message("text.retry_collection_for") + ", ".join(map(str, result.errors)))
                previous_errors = result.errors
                disconnected = False
            except RuntimeEnded:
                record_event(project, "connection", "runtime_ended", runtime_id=session.runtime_id)
                return True
            except KomaquillError as error:
                if not disconnected:
                    record_event(
                        project,
                        "connection",
                        "disconnected",
                        runtime_id=session.runtime_id,
                        failure=failure_info(error, "connection"),
                    )
                    echo(message("text.checking_the_connection_pending_results_will_be_collected"))
                disconnected = True
            time.sleep(project.settings().poll_interval)
        return stop_request(project, session) is True
    except KeyboardInterrupt:
        echo(message("text.waiting_for_results_press_ctrl_c_again_to"))
        request_stop(project, session, False)
        return False


def supervise(
    project: Project,
    client: ComfyClient,
    session: Session,
    echo: Callable[[str], None],
    on_ready: Callable[[Session], None] | None,
    reconnect: Callable[[], None] | None = None,
) -> bool:
    """Restore preserved materials before opening a ready session."""
    coordinator = Coordinator(project, client) if capabilities(client) else None
    try:
        if coordinator is not None:
            coordinator.materials.restore()
        if on_ready is not None:
            on_ready(session)
        if coordinator is None:
            return (
                watch(project, client, session, echo, reconnect)
                if reconnect
                else watch(project, client, session, echo)
            )
        return watch(project, client, session, echo, reconnect, coordinator)
    finally:
        if coordinator is not None:
            try:
                coordinator.close()
            except KomaquillError as error:
                record_event(
                    project,
                    "preparation",
                    "stop_failed",
                    runtime_id=session.runtime_id,
                    failure=failure_info(error, "preparation"),
                )


def preserve(project: Project, client: ComfyClient | None) -> tuple[str, ...] | None:
    """Retry material collection independently of generation and GPU termination."""
    if client is None or not capabilities(client):
        return None
    collector = MaterialCollector(project, client)
    failures = collector.collect()
    return tuple(failures)
