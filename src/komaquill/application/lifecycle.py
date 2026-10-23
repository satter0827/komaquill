"""Finalize owned runtimes through explicit, replaceable shutdown actions."""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from komaquill.application.generation import Collection
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.domain.models import RuntimeObservation, ShutdownResult


@dataclass(frozen=True)
class ShutdownActions:
    """Bind side effects without exposing provider, process or filesystem objects."""

    collect: Callable[[], Collection | None]
    logs: Callable[[], tuple[str, ...]]
    stop: Callable[[], RuntimeObservation]
    disconnect: Callable[[], None]
    finalize: Callable[[], tuple[UUID, ...]]
    persist: Callable[[ShutdownResult], None]
    release: Callable[[], None]
    display: Callable[[ShutdownResult], None]
    forced: Callable[[], bool]


def shutdown(
    runtime_id: UUID, force: bool, actions: ShutdownActions
) -> tuple[ShutdownResult, bool]:
    """Attempt every phase and release ownership only after durable finalization."""
    result = ShutdownResult(runtime_id=runtime_id, force=force)
    retain = False

    def failed(stage: str, error: BaseException, *, fatal: bool = True) -> None:
        nonlocal retain
        result.errors.append(failure_info(error, stage))
        retain = retain or fatal

    try:
        report = actions.collect()
        result.collection = "ok"
        result.force = force or actions.forced()
        if report is not None:
            result.pending_ids = list(report.pending_ids)
            if report.errors or report.failure:
                result.collection = "failed"
                result.errors.append(
                    report.failure
                    or failure_info(KomaquillError(code="collection_failed"), "collection")
                )
    except (Exception, KeyboardInterrupt) as error:
        result.collection = "failed"
        failed("collection", error)
    try:
        missing = actions.logs()
        result.logs = "failed" if missing else "ok"
        if missing:
            failed("log_collection", KomaquillError(code="log_unavailable"), fatal=False)
    except (Exception, KeyboardInterrupt) as error:
        result.logs = "failed"
        failed(
            "log_collection",
            error,
            fatal=not isinstance(error, (KomaquillError, OSError, KeyboardInterrupt)),
        )
    try:
        try:
            observation = actions.stop()
        except KeyboardInterrupt:
            observation = actions.stop()
        if not isinstance(observation, RuntimeObservation) or not observation.stopped:
            raise KomaquillError(code="colab_failed", stage="gpu_stop")
        result.gpu_observation = observation
        result.gpu = "stopped"
    except (Exception, KeyboardInterrupt) as error:
        failed("gpu_stop", error)
    try:
        actions.disconnect()
    except (Exception, KeyboardInterrupt) as error:
        result.connections = "failed"
        failed("disconnect", error)
    if result.gpu == "stopped":
        try:
            result.lost_ids = list(actions.finalize())
            result.history = "ok"
            if result.lost_ids:
                result.collection = "incomplete"
                if not result.force:
                    failed(
                        "collection",
                        KomaquillError(code="shutdown_timeout", retryable=False),
                        fatal=False,
                    )
        except (Exception, KeyboardInterrupt) as error:
            result.history = "failed"
            failed("finalize", error)
    result.finished_at = datetime.now(UTC)
    try:
        actions.display(result)
    except (Exception, KeyboardInterrupt) as error:
        failed("display", error)
    try:
        actions.persist(result)
    except (Exception, KeyboardInterrupt) as error:
        failed("summary", error)
    released = False
    if result.gpu == "stopped" and not retain:
        try:
            actions.release()
            released = True
        except (Exception, KeyboardInterrupt) as error:
            failed("release", error)
    if retain:
        with suppress(Exception, KeyboardInterrupt):
            actions.persist(result)
    return result, released
