"""Verify platform contracts within the declared test scope."""

import os
from types import SimpleNamespace

import psutil
import pytest
from komaquill.adapters import file_users as probe


@pytest.mark.parametrize(
    "mode", ["empty", "busy", "start", "register", "list", "end", "load", "racing", "unavailable"]
)
def test_restart_manager_closes_sessions_and_preserves_unknown(monkeypatch, tmp_path, mode):
    calls = []

    def start(handle, flags, key):
        calls.append("start")
        handle._obj.value = 5
        return 1 if mode == "start" else 0

    def register(*args):
        calls.append("register")
        assert args[1] == 1 and args[2][0] == str(tmp_path)
        return 1 if mode == "register" else 0

    def query(session, needed, count, buffer, reasons):
        calls.append("list")
        if mode == "list":
            return 1
        if mode == "empty":
            return 0
        if buffer is None or mode == "racing":
            needed._obj.value = 1
            return probe.ERROR_MORE_DATA
        buffer[0].process.pid = 99999
        count._obj.value = 1
        return 0

    def end(*args):
        calls.append("end")
        return 1 if mode == "end" else 0

    def load(*args, **kwargs):
        if mode == "load":
            raise OSError("private system failure")
        return SimpleNamespace(
            RmStartSession=start, RmRegisterResources=register, RmGetList=query, RmEndSession=end
        )

    monkeypatch.setattr(
        probe.ctypes, "WinDLL", None if mode == "unavailable" else load, raising=False
    )
    assert probe.windows_users([tmp_path]) == (
        set() if mode == "empty" else {99999} if mode == "busy" else None
    )
    assert ("end" in calls) is (mode not in {"load", "start", "unavailable"})
    assert calls.count("list") <= probe.RM_QUERY_ATTEMPTS


@pytest.mark.parametrize("case", ["busy", "other", "gone", "unknown", "self", "not_python"])
def test_posix_file_checks_distinguish_use_and_uncertainty(tmp_path, monkeypatch, case):
    target = tmp_path / "data.db"
    target.touch()

    def files():
        if case == "gone":
            raise psutil.NoSuchProcess(99999)
        if case == "unknown":
            raise psutil.AccessDenied(99999)
        return [SimpleNamespace(path=str(target if case == "busy" else tmp_path / "other"))]

    process = SimpleNamespace(
        pid=os.getpid() if case == "self" else 99999,
        info={"name": "node" if case == "not_python" else "python3"},
        open_files=files,
    )
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe.psutil, "process_iter", lambda _: [process])
    assert probe.file_users([target]) == (
        {99999} if case == "busy" else None if case == "unknown" else set()
    )


def test_empty_selection_does_not_load_platform_apis(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "windows_users", lambda *_: pytest.fail("unexpected query"))
    assert probe.file_users([tmp_path / "absent"]) == set()
