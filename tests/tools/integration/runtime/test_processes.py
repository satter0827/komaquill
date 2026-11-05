"""Check real process exit codes and bounded cleanup of owned descendants."""

import os
import subprocess
import sys
from contextlib import suppress

import psutil
import pytest

from tools.runtime.processes import run_owned


def test_owned_tool_preserves_output_and_exit_code(tmp_path):
    output = tmp_path / "output.log"
    with output.open("w") as handle:
        result = run_owned(
            [sys.executable, "-c", "print('fixture output'); raise SystemExit(7)"],
            cwd=tmp_path,
            env=os.environ,
            stdout=handle,
            timeout=10,
        )
    assert result == 7
    assert output.read_text().strip() == "fixture output"


def test_timeout_terminates_owned_child_without_touching_other_processes(tmp_path):
    child = tmp_path / "child.pid"
    source = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ) as unrelated:
        try:
            with (
                (tmp_path / "output.log").open("w") as output,
                pytest.raises(subprocess.TimeoutExpired),
            ):
                run_owned(
                    [sys.executable, "-c", source, str(child)],
                    cwd=tmp_path,
                    env=os.environ,
                    stdout=output,
                    timeout=3,
                )
            assert child.is_file()
            with suppress(psutil.NoSuchProcess):
                psutil.Process(int(child.read_text())).wait(timeout=1)
            assert unrelated.poll() is None
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)


def test_normal_parent_exit_does_not_leave_an_owned_child(tmp_path):
    import json

    child = tmp_path / "child.json"
    source = (
        "import subprocess,sys,json,psutil; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(json.dumps([p.pid,psutil.Process(p.pid).create_time()]))"
    )
    try:
        with (tmp_path / "output.log").open("w") as output:
            assert (
                run_owned(
                    [sys.executable, "-c", source, str(child)],
                    cwd=tmp_path,
                    env=os.environ,
                    stdout=output,
                    timeout=10,
                )
                == 0
            )
        pid, started = json.loads(child.read_text())
        with suppress(psutil.NoSuchProcess):
            owned = psutil.Process(pid)
            if owned.create_time() == started and (
                sys.platform == "win32" or owned.status() != psutil.STATUS_ZOMBIE
            ):
                owned.wait(timeout=0)
    finally:
        if child.is_file():
            pid, started = json.loads(child.read_text())
            with suppress(psutil.NoSuchProcess):
                owned = psutil.Process(pid)
                if owned.create_time() == started:
                    try:
                        owned.wait(timeout=0)
                    except psutil.TimeoutExpired:
                        if owned.status() != psutil.STATUS_ZOMBIE:
                            owned.kill()
                            owned.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract")
def test_job_assignment_failure_terminates_the_suspended_parent(tmp_path, monkeypatch):
    import win32job

    observed = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        observed.append(process)
        return process

    def fail(*_):
        raise RuntimeError("Injected job assignment failure")

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(win32job, "AssignProcessToJobObject", fail)
    marker = tmp_path / "executed"
    with (tmp_path / "output.log").open("w") as output, pytest.raises(RuntimeError):
        run_owned(
            [sys.executable, "-c", "from pathlib import Path; Path('executed').touch()"],
            cwd=tmp_path,
            env=os.environ,
            stdout=output,
            timeout=10,
        )
    assert observed and all(process.poll() is not None for process in observed)
    assert not marker.exists()
