"""Bound a tool invocation and clean up its owned descendants on interruption."""

import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO

import psutil

from tools.reporting import message

TERMINATION_TIMEOUT_SECONDS = 3.0


@contextmanager
def owned_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout: IO[str],
) -> Iterator[subprocess.Popen[str]]:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        text=True,
    ) as process:
        try:
            owner = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            owner = None
        try:
            yield process
        finally:
            if owner is not None and owner.is_running():
                owned = [owner]
                with suppress(psutil.NoSuchProcess):
                    owned.extend(owner.children(recursive=True))
                for child in reversed(owned):
                    with suppress(psutil.NoSuchProcess):
                        child.terminate()
                _, remaining = psutil.wait_procs(owned, timeout=TERMINATION_TIMEOUT_SECONDS)
                for child in remaining:
                    with suppress(psutil.NoSuchProcess):
                        child.kill()
                _, remaining = psutil.wait_procs(remaining, timeout=TERMINATION_TIMEOUT_SECONDS)
                if remaining:
                    raise RuntimeError(message("quality.termination_failed")) from None


def run_owned(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdout: IO[str], timeout: float
) -> int:
    with owned_process(command, cwd=cwd, env=env, stdout=stdout) as process:
        return process.wait(timeout=timeout)
