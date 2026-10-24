"""Verify atomic replacement through transient Windows sharing conflicts."""

import sys
import threading
from pathlib import Path

import pytest

from komaquill.infrastructure import atomic as storage


@pytest.mark.parametrize("case", ["sharing", "permanent", "posix", "unknown"])
def test_atomic_publication_retries_only_bounded_windows_conflicts(tmp_path, monkeypatch, case):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    original = Path.replace
    calls = []
    ticks = [0.0]
    monkeypatch.setattr(storage.sys, "platform", "linux" if case == "posix" else "win32")
    monkeypatch.setattr(storage.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        storage.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds)
    )
    monkeypatch.setattr(storage, "REPLACE_TIMEOUT_SECONDS", 0.1)

    def replace(source, destination):
        calls.append(True)
        if case == "sharing" and len(calls) > 1:
            return original(source, destination)
        error = PermissionError("not logged")
        error.winerror = 123 if case == "unknown" else 32
        raise error

    monkeypatch.setattr(Path, "replace", replace)
    if case == "sharing":
        storage.atomic_bytes(target, b"new")
        assert target.read_bytes() == b"new" and len(calls) == 2
    else:
        with pytest.raises(PermissionError):
            storage.atomic_bytes(target, b"new")
        assert target.read_bytes() == b"old"
        assert len(calls) == (3 if case == "permanent" else 1)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing semantics")
def test_a_real_windows_reader_does_not_stop_atomic_state_updates(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    reader = target.open("rb")
    try:
        with pytest.raises(PermissionError):
            target.replace(tmp_path / "blocked.json")
        timer = threading.Timer(0.1, reader.close)
        timer.start()
        try:
            storage.atomic_bytes(target, b"new")
        finally:
            timer.join()
        assert target.read_bytes() == b"new"
    finally:
        reader.close()
