"""Check real process exit codes and bounded cleanup of owned descendants."""

import os
import subprocess
import sys

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
            assert not psutil.pid_exists(int(child.read_text()))
            assert unrelated.poll() is None
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)
