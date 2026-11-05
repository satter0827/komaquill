"""Bound a tool invocation and clean up its owned descendants on interruption."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import IO, cast

import psutil

from tools.reporting import message

TERMINATION_TIMEOUT_SECONDS = 3.0
TERMINATION_POLL_SECONDS = 0.02


@contextmanager
def owned_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout: IO[str],
) -> Iterator[subprocess.Popen[str]]:
    with ExitStack() as stack:
        flags = 0
        if sys.platform == "win32":
            import win32api
            import win32con
            import win32job
            import win32process

            # The upstream stub incorrectly declares this handle return as None.
            create_job = cast(Callable[[None, str], int], win32job.CreateJobObject)
            job = create_job(None, "")
            stack.callback(win32api.CloseHandle, job)
            limits = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, limits
            )
            flags = subprocess.CREATE_NO_WINDOW | win32process.CREATE_SUSPENDED
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
            text=True,
        ) as process:
            try:
                if sys.platform == "win32":
                    # Assign before resuming so every descendant inherits this job.
                    handle = win32api.OpenProcess(
                        win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                        False,
                        process.pid,
                    )
                    try:
                        win32job.AssignProcessToJobObject(job, handle)
                    finally:
                        win32api.CloseHandle(handle)
                    psutil.Process(process.pid).resume()
                yield process
            finally:
                if sys.platform == "win32":
                    win32job.TerminateJobObject(job, 1)
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
                    deadline = time.monotonic() + TERMINATION_TIMEOUT_SECONDS
                    while win32job.QueryInformationJobObject(
                        job, win32job.JobObjectBasicAccountingInformation
                    )["ActiveProcesses"]:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(message("quality.termination_failed")) from None
                        time.sleep(TERMINATION_POLL_SECONDS)
                else:
                    # The process group remains owned after the initial parent exits.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)


def run_owned(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdout: IO[str], timeout: float
) -> int:
    with owned_process(command, cwd=cwd, env=env, stdout=stdout) as process:
        return process.wait(timeout=timeout)
