"""Enforce optional compute-unit limits independently of generation callers."""

from datetime import UTC, datetime

from filelock import FileLock

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.usage import ComputeBudget
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event

OBSERVATION_INTERVAL_SECONDS = 20.0
MAXIMUM_OBSERVATION_AGE_SECONDS = 60.0


def enforce(project: Project, *, allocation_pending: bool = False) -> str | None:
    """Stop on uncertain ownership, balance increases or exhausted reserves."""
    settings = project.settings()
    if not isinstance(settings, RuntimeSettings) or settings.compute_unit_limit is None:
        return None
    from komaquill.infrastructure.colab import Colab

    colab = Colab(project)
    receipt = colab.receipt()
    expected = int(receipt is not None and receipt.endpoint_id is not None)
    pending = allocation_pending and receipt is not None and receipt.endpoint_id is None
    path = project.state / "compute-budget.json"
    lock = FileLock(str(path) + ".lock")
    now = datetime.now(UTC)
    with lock:
        prior = ComputeBudget.model_validate_json(path.read_bytes()) if path.exists() else None
    if prior is not None:
        if (prior.limit, prior.reserve) != (
            settings.compute_unit_limit,
            settings.compute_unit_reserve,
        ):
            raise KomaquillError(
                code="invalid_configuration", stage="colab_control", retryable=False
            )
        if (
            prior.latest.assignments_count == expected
            and 0 <= (now - prior.latest.observed_at).total_seconds() < OBSERVATION_INTERVAL_SECONDS
        ):
            if not prior.permits(
                settings.shutdown_timeout + settings.control_timeout,
                now,
                MAXIMUM_OBSERVATION_AGE_SECONDS,
            ):
                raise KomaquillError(code="stopping", stage="colab_control", retryable=False)
            return prior.latest.account_sha256
    usage = colab.usage()
    if not record_event(
        project,
        "compute_usage",
        "observed",
        runtime_id=receipt.runtime_id if receipt else None,
        usage=usage,
    ):
        raise KomaquillError(code="diagnostics_failed", stage="colab_control", retryable=False)
    if (
        not 0
        <= (datetime.now(UTC) - usage.observed_at).total_seconds()
        <= MAXIMUM_OBSERVATION_AGE_SECONDS
    ):
        raise KomaquillError(code="invalid_response", stage="colab_control", retryable=False)
    if (
        usage.assignments_count not in {0, 1} if pending else usage.assignments_count != expected
    ) or (receipt is not None and receipt.account_sha256 != usage.account_sha256):
        raise KomaquillError(code="identity_mismatch", stage="colab_control", retryable=False)
    if expected and (receipt is None or not colab.refresh(receipt)):
        raise KomaquillError(code="identity_mismatch", stage="colab_control", retryable=False)
    with lock:
        latest = ComputeBudget.model_validate_json(path.read_bytes()) if path.exists() else None
        if latest is not None and usage.account_sha256 != latest.latest.account_sha256:
            raise KomaquillError(code="identity_mismatch", stage="colab_control", retryable=False)
        if latest is not None and usage.observed_at < latest.latest.observed_at:
            budget = latest
        else:
            if latest is not None and (
                usage.remaining_compute_units > latest.latest.remaining_compute_units
            ):
                raise KomaquillError(
                    code="invalid_response", stage="colab_control", retryable=False
                )
            budget = ComputeBudget(
                limit=settings.compute_unit_limit,
                reserve=settings.compute_unit_reserve,
                initial_balance=latest.initial_balance if latest else usage.remaining_compute_units,
                latest=usage,
            )
            atomic_json(path, budget.model_dump(mode="json"))
    if not budget.permits(
        settings.shutdown_timeout + settings.control_timeout,
        datetime.now(UTC),
        MAXIMUM_OBSERVATION_AGE_SECONDS,
    ):
        raise KomaquillError(code="stopping", stage="colab_control", retryable=False)
    return budget.latest.account_sha256
