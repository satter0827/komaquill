"""Keep accepted HTTP responses separate from retained execution failures."""

from collections.abc import Callable
from uuid import UUID

from komaquill.domain.errors import KomaquillError


def execute_retained(execute: Callable[[UUID], object], identity: UUID) -> None:
    try:
        execute(identity)
    except KomaquillError:
        # The operation lock is released; its checkpoint is recovered by status reads.
        return
