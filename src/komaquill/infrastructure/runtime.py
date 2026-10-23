"""Maintain the connection, collection and shutdown lifecycle."""

import subprocess
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import keyring
from filelock import FileLock, Timeout
from pydantic import ValidationError

from komaquill.application.generation import (
    Collection,
    collect,
    collection_lock,
    mark_lost,
    verify_runtime,
)
from komaquill.application.lifecycle import ShutdownActions, shutdown
from komaquill.domain.errors import KomaquillError, RuntimeEnded, failure_info
from komaquill.domain.models import Session, ShutdownResult
from komaquill.infrastructure.colab import Colab, Receipt, check_port, terminate
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.deployment import create_payload
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.downloads import fetch_model
from komaquill.infrastructure.materials import MaterialCollector, finalize_materials
from komaquill.infrastructure.monitor_state import (
    cancel_requested,
    check_cancel,
    read_monitor,
    update_monitor,
)
from komaquill.infrastructure.preparation import Coordinator, capabilities, verify_extension
from komaquill.infrastructure.storage import atomic_json, load_runs, load_session, stop_request
from komaquill.infrastructure.workflows import assets
from komaquill.messages import message

PROGRESS_POLL_SECONDS = 10.0


def session_project(project: Project) -> Project:
    """Use saved settings for status and stop even if the source file is broken."""
    try:
        session = load_session(project)
    except (OSError, ValueError):
        return project
    return project.configured(session.settings) if session.settings is not None else project


def credential_key(project: Project, session: Session) -> str:
    """Scope credential storage to the workspace, endpoint and username."""
    return f"{project.root}|{session.endpoint}|{session.username}"


@contextmanager
def connected(project: Project) -> Iterator[tuple[ComfyClient, Session]]:
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


def monitor_lock(project: Project) -> FileLock:
    """Prevent concurrent startup and teardown."""
    project.state.mkdir(parents=True, exist_ok=True)
    return FileLock(str(project.state / ".monitor.lock"))


def excluded_history(client: ComfyClient) -> set[str]:
    """Exclude queue and history entries that precede the connection."""
    excluded = {str(row[1]) for row in client.queue()}
    excluded.update(client.history())
    return excluded


def request_stop(project: Project, session: Session, force: bool) -> None:
    """Keep forced shutdown requests monotonic across processes."""
    with collection_lock(project), FileLock(str(project.state / ".stop.lock")):
        force = force or stop_request(project, session) is True
        atomic_json(
            project.state / "stop.json", {"runtime_id": str(session.runtime_id), "force": force}
        )


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
    result = Collection(0, 0, 0)
    saved = 0
    previous_errors: tuple[UUID | str, ...] = ()
    while True:
        try:
            if cancel_requested(project) is True:
                force = True
                request_stop(project, session, True)
            try:
                result = collect(project, client, session)
                material_errors = preserve(project, client) or ()
                if material_errors:
                    result = Collection(
                        result.saved,
                        result.pending,
                        result.failed,
                        (*result.errors, *material_errors),
                        result.pending_ids,
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
    return Collection(saved, result.pending, result.failed, result.errors, result.pending_ids)


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
    try:
        while stop_request(project, session) is None:
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

    folder = project.artifacts / "logs" / str(receipt.runtime_id)
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
    result.finished_at = datetime.now(UTC)
    atomic_json(
        project.artifacts / "logs" / str(session.runtime_id) / "shutdown.json",
        result.model_dump(mode="json"),
    )
    record_event(project, "monitor", "stopped", runtime_id=session.runtime_id)
    if result.errors:
        raise KomaquillError(
            display_key="text.collection_failed_check_the_connection_and_storage_then"
        )


def start_external(
    project: Project,
    endpoint: str,
    username: str | None,
    password: str | None,
    echo: Callable[[str], None],
    on_ready: Callable[[Session], None] | None = None,
) -> None:
    """Monitor an already running ComfyUI server."""
    project = project.configured()
    session = Session(
        endpoint=endpoint,
        backend="external",
        runtime_id=uuid4(),
        username=username,
        settings=project.settings(),
    )
    if project.session_file.is_file():
        prior = load_session(project)
        if (
            prior.backend == "external"
            and prior.endpoint == session.endpoint
            and prior.username == username
        ):
            session = prior.model_copy(
                update={"username": username, "settings": project.settings()}
            )
    client = ComfyClient(
        session.endpoint, password, project.settings().request_timeout, username=username
    )
    force = True
    ready = False
    try:
        client.json("/system_stats")
        session.excluded_prompt_ids = excluded_history(client)
        if username is not None and password is not None:
            keyring.set_password("komaquill", credential_key(project, session), password)
        atomic_json(project.session_file, session.model_dump(mode="json"))
        record_event(project, "connection", "ready", runtime_id=session.runtime_id)
        echo(message("text.connected_keep_this_terminal_open_and_run_generate"))
        ready = True
        force = supervise(project, client, session, echo, on_ready)
    finally:
        try:
            if ready:
                finish_external(project, client, session, force, echo)
        finally:
            client.close()


def start_colab(
    project: Project, echo: Callable[[str], None], on_ready: Callable[[Session], None] | None = None
) -> None:
    """Own one runtime from allocation through confirmed shutdown."""
    requested = project.settings()
    colab = Colab(project)
    saved = colab.receipt()
    if saved is not None and saved.settings is not None and requested != saved.settings:
        raise KomaquillError(display_key="text.settings_differ_from_the_active_session_run_stop")
    project = project.configured(colab.settings)
    processes: list[subprocess.Popen[bytes]] = []
    tunnel: subprocess.Popen[bytes] | None = None
    client: ComfyClient | None = None
    session: Session | None = None
    monitor = read_monitor(project)
    operation_id = monitor.operation_id if monitor is not None else uuid4()
    started_at = time.monotonic()

    def phase(name: str, runtime_id: UUID | None = None, asset: str | None = None) -> None:
        update_monitor(
            project,
            phase="connected" if name == "ready" else name,
            runtime_id=runtime_id,
            asset=asset,
        )
        record_event(
            project,
            "startup",
            name,
            runtime_id=runtime_id,
            operation_id=operation_id,
            elapsed_seconds=time.monotonic() - started_at,
        )

    local_models = ThreadPoolExecutor(max_workers=1, thread_name_prefix="startup-models")
    local_preparation = []
    cleanup_owned = False
    ready = False
    force = True
    try:
        check_port(project.settings().local_port)
        if not record_event(project, "start", "preparing"):
            raise KomaquillError(
                display_key="text.cannot_initialize_diagnostics_check_the_log_directory",
                code="diagnostics_failed",
            )
        check_cancel(project)
        if saved is None:
            echo(message("text.downloading_and_verifying_the_model"))
            for asset in assets(project.settings()):
                phase("model_preparation", asset=asset.name)
                local_preparation.append(local_models.submit(fetch_model, project.artifacts, asset))
                check_cancel(project)
        phase("models_scheduled" if saved is None else "models_reused")
        payload = create_payload(project)
        phase("payload_ready")
        cleanup_owned = colab.receipt() is None
        check_cancel(project)
        receipt, created = colab.acquire(payload.code_sha256)
        phase("runtime_owned", receipt.runtime_id)
        cleanup_owned = True
        worker = None
        if created:
            echo(message("text.preparing_the_environment_and_model_on_colab"))
            worker = colab.prepare(receipt, payload)
            processes.append(worker)

        def reconnect() -> None:
            nonlocal tunnel
            if tunnel is None or tunnel.poll() is not None:
                with colab.lock:
                    if not colab.refresh(receipt):
                        raise RuntimeEnded(
                            display_key="text.colab_has_ended",
                            code="runtime_ended",
                            retryable=False,
                        )
                tunnel = colab.tunnel(receipt)
                processes.append(tunnel)

        reconnect()
        phase("ssh_connected", receipt.runtime_id)
        session = Session(
            endpoint=f"http://127.0.0.1:{project.settings().local_port}",
            runtime_id=receipt.runtime_id,
            settings=project.settings(),
        )
        client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
        deadline = time.monotonic() + project.settings().preparation_timeout
        next_progress = time.monotonic() + PROGRESS_POLL_SECONDS
        previous_progress: dict[str, str | None] = {}
        while True:
            check_cancel(project)
            if time.monotonic() >= next_progress:
                current_progress = colab.progress(receipt)
                if current_progress and current_progress != previous_progress:
                    update_monitor(
                        project,
                        phase="connected"
                        if current_progress["phase"] == "ready"
                        else current_progress["phase"],
                        asset=current_progress.get("asset"),
                    )
                    record_event(
                        project,
                        "preparation",
                        str(current_progress["phase"]),
                        runtime_id=receipt.runtime_id,
                        operation_id=operation_id,
                        asset=current_progress.get("asset"),
                    )
                    previous_progress = current_progress
                next_progress = time.monotonic() + PROGRESS_POLL_SECONDS
            try:
                metadata = client.runtime()
                session = Session.model_validate(
                    {**metadata, "endpoint": session.endpoint, "settings": project.settings()}
                )
                if session.runtime_id != receipt.runtime_id:
                    raise KomaquillError(
                        display_key="text.the_colab_runtime_identity_differs_from_ownership_records",
                        retryable=False,
                    )
                if receipt.code_sha256 is not None and session.code_sha256 != receipt.code_sha256:
                    raise KomaquillError(
                        display_key="text.the_gpu_code_differs_from_the_expected_payload",
                        retryable=False,
                    )
                verify_runtime(client, session)
                verify_extension(client)
                break
            except KomaquillError as error:
                if not error.retryable:
                    raise
                if (
                    time.monotonic() >= deadline
                    or (worker is not None and worker.poll() is not None)
                    or (tunnel is not None and tunnel.poll() is not None)
                ):
                    raise KomaquillError(
                        display_key="text.comfyui_did_not_become_ready_check_the_preparation"
                    ) from None
                time.sleep(project.settings().poll_interval)
        for future in local_preparation:
            future.result()
        phase("model_verified", receipt.runtime_id)
        check_cancel(project)
        atomic_json(project.session_file, session.model_dump(mode="json"))
        record_event(project, "connection", "ready", runtime_id=session.runtime_id)
        phase("ready", receipt.runtime_id)
        try:
            colab.recover_diagnostics(receipt)
        except (Exception, KeyboardInterrupt) as error:
            record_event(
                project,
                "log_collection",
                "failed",
                runtime_id=receipt.runtime_id,
                failure=failure_info(error, "log_collection"),
            )
        echo(message("text.connected_keep_this_terminal_open_and_run_generate"))
        ready = True
        force = supervise(project, client, session, echo, on_ready, reconnect)
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, KomaquillError) and error.code == "stopping":
            force = cancel_requested(project) is True
        if not ready:
            record_event(
                project,
                "start",
                "failed",
                runtime_id=session.runtime_id if session else None,
                failure=failure_info(error, "preparation"),
            )
            echo(message("text.comfyui_startup_failed_check_the_startup_logs_under"))
        raise
    finally:
        try:
            final_receipt = colab.receipt() if cleanup_owned else None
            if final_receipt is not None:
                finish_colab(
                    project,
                    colab,
                    final_receipt,
                    client if ready else None,
                    session if ready else None,
                    force,
                    echo,
                    processes,
                )
        finally:
            local_models.shutdown(wait=True, cancel_futures=True)
            if client is not None:
                client.close()


def start(
    project: Project,
    backend: str,
    endpoint: str | None,
    username: str | None,
    password: str | None,
    echo: Callable[[str], None],
    on_ready: Callable[[Session], None] | None = None,
) -> None:
    """Start the requested backend while preventing duplicate monitors."""
    try:
        with monitor_lock(project).acquire(timeout=0):
            (project.state / "stop.json").unlink(missing_ok=True)
            if backend == "external":
                if endpoint is None:
                    raise KomaquillError(
                        display_key="text.specify_endpoint_for_an_external_connection"
                    )
                if Colab(project).receipt() is not None:
                    raise KomaquillError(
                        display_key="text.an_owned_colab_runtime_remains_run_stop_before"
                    )
                start_external(project, endpoint, username, password, echo, on_ready)
            else:
                if endpoint is not None or username is not None:
                    raise KomaquillError(
                        display_key="text.endpoint_and_basic_authentication_options_apply_only_to"
                    )
                start_colab(project, echo, on_ready)
    except Timeout:
        raise KomaquillError(
            display_key="text.a_start_process_is_already_running_run_stop"
        ) from None


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
            path = project.artifacts / "logs" / str(expected_runtime) / "shutdown.json"
            if path.is_file() and ShutdownResult.model_validate_json(path.read_bytes()).errors:
                raise KomaquillError(
                    display_key="text.some_shutdown_phases_failed_check_the_shutdown_report"
                )
        echo(message("text.stopped_2"))
    finally:
        lock.release()
