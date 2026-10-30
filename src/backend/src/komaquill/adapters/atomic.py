"""Publish individual files atomically while preserving primary failures."""

import errno
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from komaquill.domain.models import JsonObject
from komaquill.messages import message

WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})
REPLACE_TIMEOUT_SECONDS = 1.0
REPLACE_RETRY_SECONDS = 0.05


def discard_temporary(path: Path, primary: BaseException | None) -> None:
    """Preserve a primary failure when a temporary file cannot be removed."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        if primary is None:
            raise
        print(message("storage.cleanup_failed"), file=sys.stderr)


def _sharing_retry[T](operation: Callable[[], T]) -> T:
    """Bound retries to Windows file sharing conflicts."""
    deadline = None
    while True:
        try:
            return operation()
        except PermissionError as error:
            winerror = getattr(error, "winerror", None)
            if sys.platform != "win32" or not (
                winerror in WINDOWS_REPLACE_ERRORS
                or (winerror is None and error.errno == errno.EACCES)
            ):
                raise
            now = time.monotonic()
            if deadline is None:
                deadline = now + REPLACE_TIMEOUT_SECONDS
            elif now >= deadline:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


def replace_file(source: Path, target: Path) -> None:
    """Publish an already written file through bounded Windows sharing conflicts."""
    _sharing_retry(lambda: source.replace(target))


def read_bytes(path: Path) -> bytes:
    """Read an atomically replaced snapshot through the same sharing conflicts."""
    return _sharing_retry(path.read_bytes)


def atomic_bytes(path: Path, content: bytes) -> None:
    """Replace a file using a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    committed = False
    primary: BaseException | None = None
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
        committed = True
    except BaseException as error:
        primary = error
        raise
    finally:
        if not committed:
            discard_temporary(temporary, primary)


def atomic_json(path: Path, data: JsonObject) -> None:
    """Write UTF-8 JSON atomically."""
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
