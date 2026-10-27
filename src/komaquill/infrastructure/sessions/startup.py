"""Implement session startup through explicit local adapters."""

import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import keyring
from filelock import Timeout

from komaquill.application.generation import (
    verify_runtime,
)
from komaquill.domain.configuration import remaining_seconds
from komaquill.domain.errors import KomaquillError, RuntimeEnded, failure_info
from komaquill.domain.models import Session
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab, check_port
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.deployment import create_payload
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.model_auth import preflight
from komaquill.infrastructure.monitor_state import (
    cancel_requested,
    check_cancel,
    read_monitor,
    update_monitor,
)
from komaquill.infrastructure.preparation import verify_extension
from komaquill.infrastructure.sessions.connection import (
    credential_key,
    excluded_history,
    monitor_lock,
)
from komaquill.infrastructure.sessions.monitor import supervise
from komaquill.infrastructure.sessions.shutdown import finish_colab, finish_external
from komaquill.infrastructure.storage import load_session
from komaquill.infrastructure.workflows import assets
from komaquill.messages import message

PROGRESS_POLL_SECONDS = 10.0


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
    primary_error: BaseException | None = None
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
    except (Exception, KeyboardInterrupt) as error:
        primary_error = error
        raise
    finally:
        try:
            if ready:
                try:
                    finish_external(project, client, session, force, echo)
                except (Exception, KeyboardInterrupt):
                    if primary_error is None:
                        raise
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

    cleanup_owned = False
    ready = False
    force = True
    primary_error: BaseException | None = None
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
            preflight(list(assets(project.settings())), project.settings().request_timeout)
            check_cancel(project)
        phase("access_verified" if saved is None else "runtime_reused")
        payload = create_payload(project)
        phase("payload_ready")
        cleanup_owned = colab.receipt() is None
        check_cancel(project)
        from komaquill.infrastructure.compute_budget import enforce

        expected_account = enforce(project)
        if expected_account is None:
            receipt, created = colab.acquire(payload.code_sha256)
        else:
            receipt, created = colab.acquire(payload.code_sha256, expected_account=expected_account)
        phase("runtime_owned", receipt.runtime_id)
        cleanup_owned = True
        if created or not project.session_file.is_file():
            echo(message("text.preparing_the_environment_and_model_on_colab"))
            colab.prepare(receipt, payload)

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
            started_at=receipt.started_at,
        )
        client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
        timeout = project.settings().preparation_timeout
        remaining = remaining_seconds(
            receipt.started_at, project.settings().submission_timeout, datetime.now(UTC)
        )
        if remaining is not None:
            timeout = min(timeout, remaining)
        deadline = time.monotonic() + timeout
        next_progress = time.monotonic() + PROGRESS_POLL_SECONDS
        previous_progress: dict[str, str | None] = {}
        while True:
            check_cancel(project)
            if time.monotonic() >= deadline:
                raise KomaquillError(
                    display_key="text.comfyui_did_not_become_ready_check_the_preparation",
                    code="preparation_timeout",
                    stage="preparation",
                    retryable=False,
                )
            update_monitor(project)
            if time.monotonic() >= next_progress:
                current_progress = colab.progress(receipt)
                if failure_code := current_progress.get("failure_code"):
                    raise KomaquillError(
                        code=failure_code,
                        stage=current_progress.get("failure_stage") or "preparation",
                        retryable=False,
                    )
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
                if time.monotonic() >= deadline or (
                    tunnel is not None and tunnel.poll() is not None
                ):
                    failed_progress = colab.progress(receipt)
                    if failure_code := failed_progress.get("failure_code"):
                        raise KomaquillError(
                            code=failure_code,
                            stage=failed_progress.get("failure_stage") or "preparation",
                            retryable=False,
                        ) from None
                    raise KomaquillError(
                        display_key="text.comfyui_did_not_become_ready_check_the_preparation",
                        code="preparation_timeout",
                        stage="preparation",
                        retryable=False,
                    ) from None
                time.sleep(
                    min(project.settings().poll_interval, max(0, deadline - time.monotonic()))
                )
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
        primary_error = error
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
                try:
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
                except (Exception, KeyboardInterrupt):
                    if primary_error is None:
                        raise
        finally:
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
