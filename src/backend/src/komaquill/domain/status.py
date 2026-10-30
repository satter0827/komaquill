"""Describe observed session state without presentation or provider dependencies."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SessionStatus:
    """Keep unavailable observations distinct from an empty queue."""

    gpu: str = "absent"
    collector_running: bool = False
    connection: str | None = None
    monitor_phase: str | None = None
    observed_at: datetime | None = None
    preparation_count: int | None = 0
    material_count: int | None = 0
    pending: list[str] = field(default_factory=list)
    lost_count: int = 0
    unreadable: list[str] = field(default_factory=list)
