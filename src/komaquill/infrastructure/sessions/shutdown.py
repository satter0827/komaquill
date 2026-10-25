"""Implement session shutdown through explicit local adapters."""

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from filelock import Timeout
from pydantic import ValidationError

from komaquill.application.generation import (
    Collection,
    mark_lost,
    verify_runtime,
)
from komaquill.application.lifecycle import ShutdownActions, shutdown
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.domain.models import Session, ShutdownResult
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab, Receipt, check_port, terminate
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.materials import finalize_materials
from komaquill.infrastructure.monitor_state import (
    update_monitor,
)
from komaquill.infrastructure.sessions.connection import (
    connected,
    monitor_lock,
    request_stop,
    session_project,
)
from komaquill.infrastructure.sessions.monitor import drain, preserve
from komaquill.infrastructure.storage import load_session, stop_request
from komaquill.infrastructure.stories.publication import refresh_saved_stories
from komaquill.messages import message


def finish_colab(
    project: Project,
    colab: Colab,
    receipt: Receipt,
    client: ComfyClient | None,
    session: Session | None,
    force: bool,
    echo: Callable[[str], None],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    """Bind owned resources to the provider-independent shutdown coordinator."""
    project = project.configured(receipt.settings)

    def collect_available() -> Collection | None:
        if client is None or session is None:
            return None
        update_monitor(project, phase="stopping")
        request_stop(project, session, force)
        return drain(project, client, session, force, echo)

    def disconnect() -> None:
        errors = []
        for process in processes:
            try:
                terminate(process, project.settings().control_timeout)
            except (Exception, KeyboardInterrupt) as error:
                errors.append(error)
        try:
            colab.cleanup_processes(receipt)
        except (Exception, KeyboardInterrupt) as error:
            errors.append(error)
        if errors:
            raise errors[0]

    def display(result: ShutdownResult) -> None:
        echo(message("stories.publication_status", status=result.publications))
        if result.unpublished_ids:
            echo(
                message("stories.publication_pending", identities=", ".join(result.unpublished_ids))
            )
        echo(message("materials.shutdown", result=result.materials))
        if result.lost_materials:
            echo(message("materials.lost", identities=", ".join(result.lost_materials)))
        if result.lost_ids:
            echo(
                message("text.lost_lost_ids_count", lost_ids_count=len(result.lost_ids))
                + ", ".join(map(str, result.lost_ids))
            )
        labels = {
            "ok": message("text.complete"),
            "failed": message("text.failed"),
            "skipped": message("text.skipped"),
            "incomplete": message("text.incomplete"),
            "stopped": message("text.stopped"),
            "unknown": message("text.unknown"),
        }
        echo(
            message(
                "text.collection_collection_logs_logs_colab_gpu",
                collection=labels[result.collection],
                logs=labels[result.logs],
                gpu=labels[result.gpu],
            )
        )

    folder = project.logs / str(receipt.runtime_id)
    actions = ShutdownActions(
        collect=collect_available,
        logs=lambda: colab.recover_diagnostics(receipt),
        stop=colab.stop,
        disconnect=disconnect,
        finalize=lambda: mark_lost(project, receipt.runtime_id),
        persist=lambda result: atomic_json(
            folder / "shutdown.json", result.model_dump(mode="json")
        ),
        release=lambda: colab.release(receipt),
        display=display,
        forced=lambda: session is not None and stop_request(project, session) is True,
        preserve=lambda: preserve(project, client),
        finalize_materials=lambda: finalize_materials(project, receipt.runtime_id),
        publish=lambda: refresh_saved_stories(project),
    )
    result, released = shutdown(receipt.runtime_id, force, actions)
    for failure in result.errors:
        record_event(
            project, failure.stage, "failed", runtime_id=receipt.runtime_id, failure=failure
        )
    if result.errors:
        stages = ", ".join(failure.stage for failure in result.errors)
        raise KomaquillError(
            display_key="shutdown.failed",
            details={
                "stages": stages,
                "next_action": message("shutdown.released" if released else "shutdown.retained"),
            },
            code="collection_failed",
        )


def finish_external(
    project: Project,
    client: ComfyClient,
    session: Session,
    force: bool,
    echo: Callable[[str], None],
) -> None:
    """Finalize local monitoring without stopping or invalidating the external server."""
    project = project.configured(session.settings)
    result = ShutdownResult(
        runtime_id=session.runtime_id, force=force, gpu="not_managed", logs="not_managed"
    )
    try:
        request_stop(project, session, force)
        report = drain(project, client, session, force, echo)
        result.collection = "ok"
        result.history = "ok"
        if report is not None:
            result.pending_ids = list(report.pending_ids)
            if report.failure or report.errors:
                result.collection = "failed"
                result.errors.append(
                    report.failure
                    or failure_info(KomaquillError("", code="collection_failed"), "collection")
                )
    except (Exception, KeyboardInterrupt) as error:
        result.collection = "failed"
        result.errors.append(failure_info(error, "collection"))
    result.unpublished_ids = list(map(str, refresh_saved_stories(project)))
    result.publications = "failed" if result.unpublished_ids else "ok"
    if result.unpublished_ids:
        result.errors.append(
            failure_info(KomaquillError(code="publication_failed", stage="finalize"), "finalize")
        )
    echo(message("stories.publication_status", status=result.publications))
    if result.unpublished_ids:
        echo(message("stories.publication_pending", identities=", ".join(result.unpublished_ids)))
    result.finished_at = datetime.now(UTC)
    atomic_json(
        project.logs / str(session.runtime_id) / "shutdown.json",
        result.model_dump(mode="json"),
    )
    record_event(project, "monitor", "stopped", runtime_id=session.runtime_id)
    if result.errors:
        raise KomaquillError(
            display_key="text.collection_failed_check_the_connection_and_storage_then"
        )


def stop(project: Project, force: bool, echo: Callable[[str], None]) -> None:
    """Request active monitor shutdown or recover and stop a remaining owned runtime."""
    project = session_project(project)
    lock = monitor_lock(project)
    had_monitor = False
    expected_runtime: UUID | None = None
    try:
        lock.acquire(timeout=0)
    except Timeout:
        had_monitor = True
        if not project.session_file.is_file():
            raise KomaquillError(
                display_key="text.startup_is_in_progress_press_ctrl_c_in"
            ) from None
        session = load_session(project)
        project = project.configured(session.settings)
        current_receipt = Colab(project).receipt()
        if current_receipt is not None and current_receipt.runtime_id != session.runtime_id:
            raise KomaquillError(
                display_key="text.startup_is_in_progress_press_ctrl_c_in"
            ) from None
        expected_runtime = session.runtime_id
        request_stop(project, session, force)
        echo(message("text.stop_requested_waiting_for_generation_and_collection"))
        try:
            lock.acquire(
                timeout=project.settings().shutdown_timeout + project.settings().control_timeout
            )
        except Timeout:
            raise KomaquillError(
                display_key="text.shutdown_is_still_in_progress_check_status"
            ) from None
    try:
        colab = Colab(project)
        receipt = colab.receipt()
        if (
            expected_runtime is not None
            and receipt is not None
            and receipt.runtime_id != expected_runtime
        ):
            raise KomaquillError(
                display_key="text.the_connection_changed_the_shutdown_target_is_unconfirmed"
            )
        if receipt is not None:
            project = project.configured(receipt.settings)
            processes: list[subprocess.Popen[bytes]] = []
            session = Session(
                endpoint=f"http://127.0.0.1:{project.settings().local_port}",
                runtime_id=receipt.runtime_id,
                settings=project.settings(),
            )
            client: ComfyClient | None = None
            active = True
            try:
                if project.session_file.is_file():
                    try:
                        saved_session = load_session(project)
                        if (
                            saved_session.backend == "colab"
                            and saved_session.runtime_id == receipt.runtime_id
                        ):
                            session = saved_session
                    except (OSError, ValidationError):
                        echo(
                            message(
                                "text.connection_metadata_is_unreadable_using_ownership_records_for"
                            )
                        )
                try:
                    with colab.lock:
                        active = colab.refresh(receipt)
                except KomaquillError:
                    echo(message("text.cannot_confirm_colab_status_retrying_shutdown"))
                client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
                if active:
                    try:
                        verify_runtime(client, session)
                    except KomaquillError:
                        try:
                            check_port(project.settings().local_port)
                            processes.append(colab.tunnel(receipt))
                        except (KomaquillError, OSError, subprocess.SubprocessError):
                            echo(
                                message(
                                    "text.cannot_restore_the_connection_collecting_available_logs_before"
                                )
                            )
            finally:
                try:
                    finish_colab(
                        project,
                        colab,
                        receipt,
                        client if active else None,
                        session if active else None,
                        force,
                        echo,
                        processes,
                    )
                finally:
                    if client is not None:
                        client.close()
        elif not had_monitor and project.session_file.is_file():
            session = load_session(project)
            if session.backend == "external":
                with connected(project) as (client, session):
                    finish_external(project, client, session, force, echo)
        if had_monitor and expected_runtime is not None:
            path = project.logs / str(expected_runtime) / "shutdown.json"
            if path.is_file() and ShutdownResult.model_validate_json(path.read_bytes()).errors:
                raise KomaquillError(
                    display_key="text.some_shutdown_phases_failed_check_the_shutdown_report"
                )
        echo(message("text.stopped_2"))
    finally:
        lock.release()
