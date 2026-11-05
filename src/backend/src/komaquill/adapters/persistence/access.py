"""Use shared access leases to exclude a stopped database replacement."""

from pathlib import Path
from typing import IO

import portalocker

from komaquill.domain.errors import KomaquillError


def acquire(
    path: Path, *, exclusive: bool = False, timeout: float = 10
) -> portalocker.Lock[IO[str]]:
    directory = path.parent.parent / "state/database-access"
    directory.mkdir(parents=True, exist_ok=True)
    lock = portalocker.Lock(
        directory / (path.name + ".lock"),
        mode="a",
        flags=(portalocker.LOCK_EX if exclusive else portalocker.LOCK_SH) | portalocker.LOCK_NB,
        timeout=timeout,
    )
    try:
        lock.acquire()
    except portalocker.exceptions.LockException:
        raise KomaquillError(code="operation_busy", stage="storage", retryable=False) from None
    return lock
