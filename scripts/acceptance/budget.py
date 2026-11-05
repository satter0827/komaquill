"""Account for one approved verification across owned GPU allocations."""

import hashlib
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self
from uuid import UUID

import tomli_w
from filelock import FileLock
from komaquill.api import AccountUsage
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from tools.reporting import atomic_json


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    started_at: AwareDatetime
    runtime_id: str | None = None
    stopped_at: AwareDatetime | None = None
    ownership_released: bool = False

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.stopped_at and self.stopped_at < self.started_at:
            raise ValueError("The allocation interval is reversed.")
        if self.ownership_released and self.stopped_at is None:
            raise ValueError("Released ownership requires confirmed shutdown.")
        return self


class TimeExtension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_id: UUID
    seconds: int = Field(gt=0)
    recorded_at: AwareDatetime


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
    time_extensions: list[TimeExtension] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.carried_compute_units + self.unattributed_compute_units + self.compute_unit_reserve
            >= self.compute_unit_limit
            or self.carried_images + sum(self.reservations.values()) > self.output_limit
            or any(count < 1 for count in self.reservations.values())
            or len({row.key for row in self.allocations}) != len(self.allocations)
            or any(not row.ownership_released for row in self.allocations[:-1])
            or any(
                current.started_at < previous.stopped_at
                for previous, current in zip(self.allocations, self.allocations[1:], strict=False)
                if previous.stopped_at is not None
            )
            or self.submission_seconds >= self.session_seconds
            or len({row.authorization_id for row in self.time_extensions})
            != len(self.time_extensions)
        ):
            raise ValueError("The verification budget is inconsistent.")
        return self

    @property
    def runtime_compute_unit_limit(self) -> float:
        return (
            self.compute_unit_limit - self.carried_compute_units - self.unattributed_compute_units
        )

    def used_seconds(self, now: datetime) -> float:
        if any(
            now < row.started_at
            or (row.ownership_released and (row.stopped_at is not None and now < row.stopped_at))
            for row in self.allocations
        ):
            raise ValueError("The verification clock moved behind an owned interval.")
        return sum(
            (
                (row.stopped_at if row.ownership_released and row.stopped_at is not None else now)
                - row.started_at
            ).total_seconds()
            for row in self.allocations
        )

    def remaining_seconds(self, now: datetime) -> int:
        return max(
            0,
            int(
                self.gpu_seconds
                + sum(row.seconds for row in self.time_extensions)
                - self.used_seconds(now)
            ),
        )


def path(workspace: Path) -> Path:
    return workspace / ".artifacts/state/verification-budget.json"


def read(workspace: Path) -> VerificationBudget:
    return VerificationBudget.model_validate_json(path(workspace).read_bytes())


def extend_time(workspace: Path, authorization_id: UUID, seconds: int) -> None:
    """Append an explicit time grant without resetting usage or other limits."""
    with FileLock(str(path(workspace)) + ".lock"):
        budget = read(workspace)
        previous = next(
            (row for row in budget.time_extensions if row.authorization_id == authorization_id),
            None,
        )
        if previous:
            if previous.seconds != seconds:
                raise ValueError("An existing authorization cannot change.")
            return
        if any(not row.ownership_released for row in budget.allocations):
            raise ValueError("Confirm shutdown before extending the time allowance.")
        budget.time_extensions.append(
            TimeExtension(
                authorization_id=authorization_id, seconds=seconds, recorded_at=datetime.now(UTC)
            )
        )
        atomic_json(path(workspace), budget.model_dump(mode="json"))


def open_budget(workspace: Path, limits: dict[str, Any], usage: AccountUsage) -> None:
    """Create one cumulative allowance; preserve reservations across attempts."""
    target = path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".lock"):
        if target.exists():
            budget = read(workspace)
            if any(getattr(budget, key) != value for key, value in limits.items()):
                raise ValueError("The approved verification limits cannot change.")
            if usage.account_sha256 != budget.opening_account_sha256:
                raise ValueError("The verification account changed.")
            if budget.opening_balance is None or (
                budget.opening_balance - usage.remaining_compute_units + budget.compute_unit_reserve
                >= budget.compute_unit_limit
            ):
                raise ValueError("The cumulative compute-unit budget is exhausted.")
            return
        budget = VerificationBudget(
            parent_sha256=digest(usage.model_dump(mode="json")),
            carried_compute_units=0,
            carried_images=0,
            opening_account_sha256=usage.account_sha256,
            opening_balance=usage.remaining_compute_units,
            **limits,
        )
        atomic_json(target, budget.model_dump(mode="json"))


def configure_deadlines(workspace: Path, send: int, stop: int) -> None:
    """Apply the remaining allowance before constructing the next runtime client."""
    if not 0 < send < stop:
        raise ValueError("The remaining runtime interval is invalid.")
    target = workspace / "komaquill.toml"
    settings = tomllib.loads(target.read_text(encoding="utf-8"))
    settings.update(submission_timeout=float(send), session_timeout=float(stop))
    target.write_text(tomli_w.dumps(settings), encoding="utf-8")


def initialize(
    workspace: Path, parent: dict[str, Any], limits: dict[str, Any]
) -> VerificationBudget:
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


def begin(
    workspace: Path, key: str, *, now: datetime | None = None, reserve_seconds: int | None = None
) -> tuple[int, int]:
    """Reserve an interval before explicit start; never allocate from reconnection."""
    now = now or datetime.now(UTC)
    with FileLock(str(path(workspace)) + ".lock"):
        budget = read(workspace)
        reserve = (
            budget.session_seconds - budget.submission_seconds
            if reserve_seconds is None
            else reserve_seconds
        )
        if not 0 < reserve < budget.session_seconds:
            raise ValueError("The shutdown interval is invalid.")
        active = [row for row in budget.allocations if not row.ownership_released]
        if active and active[0].key != key:
            raise ValueError("Confirm shutdown and ownership release before another allocation.")
        if any(row.key == key and row.ownership_released for row in budget.allocations):
            raise ValueError("A completed allocation cannot be restarted.")
        remaining = budget.remaining_seconds(now)
        if remaining <= reserve:
            raise ValueError("The cumulative GPU time budget is exhausted.")
        if active:
            elapsed = (now - active[0].started_at).total_seconds()
            stop = min(budget.session_seconds - int(elapsed), remaining)
            send = min(
                budget.submission_seconds - int(elapsed),
                stop - reserve,
            )
        else:
            budget.allocations.append(Allocation(key=key, started_at=now))
            stop = min(budget.session_seconds, remaining)
            send = min(
                budget.submission_seconds,
                stop - reserve,
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


def reconcile_opening_usage(workspace: Path, parent: dict[str, Any], usage: AccountUsage) -> None:
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
