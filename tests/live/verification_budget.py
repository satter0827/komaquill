"""Account for one approved verification across owned GPU allocations."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from komaquill.domain.workflow import digest
from komaquill.infrastructure.atomic import atomic_json


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    started_at: AwareDatetime
    runtime_id: str | None = None
    stopped_at: AwareDatetime | None = None
    ownership_released: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if self.stopped_at and self.stopped_at < self.started_at:
            raise ValueError("The allocation interval is reversed.")
        if self.ownership_released and self.stopped_at is None:
            raise ValueError("Released ownership requires confirmed shutdown.")
        return self


class VerificationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    parent_sha256: str
    carried_compute_units: float = Field(ge=0, allow_inf_nan=False)
    carried_images: int = Field(ge=0)
    unattributed_compute_units: float = Field(default=0, ge=0, allow_inf_nan=False)
    opening_account_sha256: str | None = None
    opening_balance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    compute_unit_limit: float = Field(gt=0, allow_inf_nan=False)
    compute_unit_reserve: float = Field(ge=0, allow_inf_nan=False)
    output_limit: int = Field(gt=0)
    gpu_seconds: int = Field(gt=0)
    session_seconds: int = Field(gt=0)
    submission_seconds: int = Field(gt=0)
    allocations: list[Allocation] = Field(default_factory=list)
    reservations: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.carried_compute_units + self.unattributed_compute_units + self.compute_unit_reserve
            >= self.compute_unit_limit
            or self.carried_images + sum(self.reservations.values()) > self.output_limit
            or any(count < 1 for count in self.reservations.values())
            or len({row.key for row in self.allocations}) != len(self.allocations)
            or any(not row.ownership_released for row in self.allocations[:-1])
            or self.submission_seconds >= self.session_seconds
        ):
            raise ValueError("The verification budget is inconsistent.")
        return self

    @property
    def runtime_compute_unit_limit(self) -> float:
        return (
            self.compute_unit_limit - self.carried_compute_units - self.unattributed_compute_units
        )

    def used_seconds(self, now: datetime) -> float:
        return sum(
            max(
                0,
                (
                    (row.stopped_at if row.ownership_released else now) - row.started_at
                ).total_seconds(),
            )
            for row in self.allocations
        )

    def remaining_seconds(self, now: datetime) -> int:
        return max(0, int(self.gpu_seconds - self.used_seconds(now)))


def path(workspace: Path) -> Path:
    return workspace / ".artifacts/state/verification-budget.json"


def read(workspace: Path) -> VerificationBudget:
    return VerificationBudget.model_validate_json(path(workspace).read_bytes())


def initialize(workspace: Path, parent: dict, limits: dict) -> VerificationBudget:
    """Carry verified usage into a new ledger without rewriting the old baseline."""
    target = path(workspace)
    with FileLock(str(target) + ".lock"):
        if target.exists():
            raise ValueError("Resume the existing verification budget.")
        previous = parent["compute_budget"]
        observed = previous["latest"]["remaining_compute_units"]
        used = previous["initial_balance"] - observed
        if used < 0:
            raise ValueError("The previous compute usage cannot be established.")
        budget = VerificationBudget(
            parent_sha256=digest(parent),
            carried_compute_units=used,
            carried_images=parent["saved_images"],
            **limits,
        )
        atomic_json(target, budget.model_dump(mode="json"))
        return budget


def begin(workspace: Path, key: str, *, now: datetime | None = None) -> tuple[int, int]:
    """Reserve an interval before explicit start; never allocate from reconnection."""
    now = now or datetime.now(UTC)
    with FileLock(str(path(workspace)) + ".lock"):
        budget = read(workspace)
        active = [row for row in budget.allocations if not row.ownership_released]
        if active and active[0].key != key:
            raise ValueError("Confirm shutdown and ownership release before another allocation.")
        if any(row.key == key and row.ownership_released for row in budget.allocations):
            raise ValueError("A completed allocation cannot be restarted.")
        remaining = budget.remaining_seconds(now)
        if remaining <= budget.session_seconds - budget.submission_seconds:
            raise ValueError("The cumulative GPU time budget is exhausted.")
        if active:
            elapsed = (now - active[0].started_at).total_seconds()
            stop = min(budget.session_seconds - int(elapsed), remaining)
            send = min(
                budget.submission_seconds - int(elapsed),
                stop - (budget.session_seconds - budget.submission_seconds),
            )
        else:
            budget.allocations.append(Allocation(key=key, started_at=now))
            stop = min(budget.session_seconds, remaining)
            send = min(
                budget.submission_seconds,
                stop - (budget.session_seconds - budget.submission_seconds),
            )
        if send <= 0:
            raise ValueError("The current allocation submission deadline has been reached.")
        atomic_json(path(workspace), budget.model_dump(mode="json"))
        return send, stop


def checkpoint(
    workspace: Path,
    key: str,
    *,
    runtime_id: str | None = None,
    stopped_at: datetime | None = None,
    released: bool = False,
) -> None:
    with FileLock(str(path(workspace)) + ".lock"):
        budget = read(workspace)
        row = next(row for row in budget.allocations if row.key == key)
        if row.runtime_id and runtime_id and row.runtime_id != runtime_id:
            raise ValueError("The owned runtime identity changed.")
        if row.ownership_released:
            if not released or stopped_at != row.stopped_at:
                raise ValueError("Confirmed shutdown cannot regress.")
            return
        row.runtime_id = runtime_id or row.runtime_id
        row.stopped_at = stopped_at
        row.ownership_released = released
        budget = VerificationBudget.model_validate(budget.model_dump())
        atomic_json(path(workspace), budget.model_dump(mode="json"))


def reserve(workspace: Path, key: str, count: int = 1, *, now: datetime | None = None) -> bool:
    with FileLock(str(path(workspace)) + ".lock"):
        budget = read(workspace)
        if key in budget.reservations:
            if budget.reservations[key] != count:
                raise ValueError("A reserved output count cannot change.")
            return False
        active = [row for row in budget.allocations if not row.ownership_released]
        now = now or datetime.now(UTC)
        if (
            not active
            or count < 1
            or (now - active[0].started_at).total_seconds() >= budget.submission_seconds
            or budget.remaining_seconds(now) <= budget.session_seconds - budget.submission_seconds
            or budget.carried_images + sum(budget.reservations.values()) + count
            > budget.output_limit
        ):
            raise ValueError("The verification submission budget is exhausted.")
        budget.reservations[key] = count
        atomic_json(path(workspace), budget.model_dump(mode="json"))
        return True


def reconcile_opening_usage(workspace: Path, parent: dict, usage) -> None:
    """Charge any unassigned balance decrease conservatively before the first start."""
    with FileLock(str(path(workspace)) + ".lock"):
        budget = read(workspace)
        previous = parent["compute_budget"]
        if (
            digest(parent) != budget.parent_sha256
            or budget.allocations
            or budget.opening_balance is not None
            or usage.assignments_count != 0
            or usage.account_sha256 != previous["latest"]["account_sha256"]
            or usage.remaining_compute_units > previous["latest"]["remaining_compute_units"]
        ):
            raise ValueError("The opening usage cannot be reconciled safely.")
        budget.unattributed_compute_units = (
            previous["latest"]["remaining_compute_units"] - usage.remaining_compute_units
        )
        budget.opening_account_sha256 = usage.account_sha256
        budget.opening_balance = usage.remaining_compute_units
        budget = VerificationBudget.model_validate(budget.model_dump())
        atomic_json(path(workspace), budget.model_dump(mode="json"))
