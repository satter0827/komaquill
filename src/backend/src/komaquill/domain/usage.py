"""Describe a fresh account observation independently of the provider SDK."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class AccountUsage(BaseModel):
    """Keep unknown or failed observations separate from a zero balance."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    remaining_compute_units: float = Field(ge=0)
    consumption_rate_hourly: float = Field(ge=0)
    assignments_count: int = Field(ge=0, strict=True)
    observed_at: AwareDatetime
    account_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ComputeBudget(BaseModel):
    """Retain one balance baseline across multiple sessions and restarts."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    format_version: Literal[1] = 1
    limit: float = Field(gt=0)
    reserve: float = Field(ge=0)
    initial_balance: float = Field(ge=0)
    latest: AccountUsage

    @property
    def consumed(self) -> float:
        return self.initial_balance - self.latest.remaining_compute_units

    def permits(self, shutdown_seconds: float, now: datetime, maximum_age: float) -> bool:
        """Reserve enough units for shutdown and reject stale observations."""
        age = (now - self.latest.observed_at).total_seconds()
        required = max(self.reserve, self.latest.consumption_rate_hourly * shutdown_seconds / 3600)
        return (
            0 <= age <= maximum_age
            and min(self.limit - self.consumed, self.latest.remaining_compute_units) > required
        )
