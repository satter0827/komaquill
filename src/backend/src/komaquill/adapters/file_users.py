"""Query users of specific files without enumerating Windows process handles."""

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

import psutil

ERROR_MORE_DATA = 234
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800
RM_QUERY_ATTEMPTS = 3
RM_SESSION_KEY_LENGTH = 32
RM_MAX_APP_NAME = 255
RM_MAX_SERVICE_NAME = 63


class UniqueProcess(ctypes.Structure):
    _fields_ = [("pid", wintypes.DWORD), ("started", wintypes.FILETIME)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", UniqueProcess),
        ("app", wintypes.WCHAR * (RM_MAX_APP_NAME + 1)),
        ("service", wintypes.WCHAR * (RM_MAX_SERVICE_NAME + 1)),
        ("kind", wintypes.UINT),
        ("status", wintypes.ULONG),
        ("session", wintypes.DWORD),
        ("restartable", wintypes.BOOL),
    ]


def windows_users(paths: list[Path]) -> set[int] | None:
    """Use only Restart Manager queries; never stop or restart applications."""
    try:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return None
        manager = loader("Rstrtmgr.dll", winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
        manager.RmStartSession.argtypes = [
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
            wintypes.LPWSTR,
        ]
        manager.RmRegisterResources.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            ctypes.POINTER(wintypes.LPCWSTR),
            wintypes.UINT,
            ctypes.POINTER(UniqueProcess),
            wintypes.UINT,
            ctypes.POINTER(wintypes.LPCWSTR),
        ]
        manager.RmGetList.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(ProcessInfo),
            ctypes.POINTER(wintypes.DWORD),
        ]
        manager.RmEndSession.argtypes = [wintypes.DWORD]
        for function in (
            manager.RmStartSession,
            manager.RmRegisterResources,
            manager.RmGetList,
            manager.RmEndSession,
        ):
            function.restype = wintypes.DWORD
        session = wintypes.DWORD()
        key = ctypes.create_unicode_buffer(RM_SESSION_KEY_LENGTH + 1)
        if manager.RmStartSession(ctypes.byref(session), 0, key):
            return None
        users: set[int] | None = None
        try:
            names = (wintypes.LPCWSTR * len(paths))(*(str(path) for path in paths))
            if not manager.RmRegisterResources(session, len(paths), names, 0, None, 0, None):
                needed, count, reasons = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
                buffer = None
                for _ in range(RM_QUERY_ATTEMPTS):
                    result = manager.RmGetList(
                        session,
                        ctypes.byref(needed),
                        ctypes.byref(count),
                        buffer,
                        ctypes.byref(reasons),
                    )
                    if result == 0:
                        if count.value == 0:
                            users = set()
                        elif buffer is not None:
                            users = {buffer[index].process.pid for index in range(count.value)}
                        break
                    if result != ERROR_MORE_DATA:
                        break
                    count.value = needed.value
                    buffer = (ProcessInfo * count.value)()
        finally:
            if manager.RmEndSession(session):
                users = None
        return users
    except OSError:
        return None


def file_users(paths: list[Path]) -> set[int] | None:
    """Return process IDs, or unknown when a safe stop check cannot be completed."""
    selected = {path.resolve() for path in paths if path.is_file()}
    if not selected:
        return set()
    if sys.platform == "win32":
        return windows_users(sorted(selected))
    users = set()
    for process in psutil.process_iter(["name"]):
        if process.pid == os.getpid() or not (process.info.get("name") or "").lower().startswith(
            ("python", "pypy")
        ):
            continue
        try:
            if any(Path(item.path).resolve() in selected for item in process.open_files()):
                users.add(process.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            return None
    return users
